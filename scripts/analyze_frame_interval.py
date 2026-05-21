#!/usr/bin/env python3
"""Analyze decoded frame images in a selected video interval.

The script is intended for forensic review before speed estimation:

- dashcam mode focuses on road markings / lane guide lines;
- surveillance mode focuses on accident vehicle or a user-provided ROI;
- optional H.264 macroblock-type stats summarize high intra-refresh frames.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Roi:
    name: str
    kind: str
    box: tuple[int, int, int, int]


MB_TOKENS = {"I", "i", "S", ">", ">-", ">|", ">+"}


def imread(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite(path: Path, img: np.ndarray) -> None:
    ext = path.suffix or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError(f"Failed to encode image: {path}")
    buf.tofile(str(path))


def parse_roi(value: str) -> Roi:
    # format: name:x1,y1,x2,y2 or name:kind:x1,y1,x2,y2
    parts = value.split(":")
    if len(parts) == 2:
        name, coords = parts
        kind = "generic"
    elif len(parts) == 3:
        name, kind, coords = parts
    else:
        raise argparse.ArgumentTypeError("ROI must be name:x1,y1,x2,y2 or name:kind:x1,y1,x2,y2")
    nums = [int(x) for x in coords.split(",")]
    if len(nums) != 4:
        raise argparse.ArgumentTypeError("ROI coordinates must be x1,y1,x2,y2")
    x1, y1, x2, y2 = nums
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError("ROI x2/y2 must be greater than x1/y1")
    if kind not in {"generic", "marking"}:
        raise argparse.ArgumentTypeError("ROI kind must be generic or marking")
    return Roi(name=name, kind=kind, box=(x1, y1, x2, y2))


def default_output_dir(video: Path | None, frames_dir: Path | None) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if video:
        return video.resolve().parent / f"{video.stem}_frame_analysis_{ts}"
    if frames_dir:
        return frames_dir.resolve().parent / f"frame_analysis_{ts}"
    return Path.cwd() / f"frame_analysis_{ts}"


def find_frame_csv(output_dir: Path, video: Path | None, explicit: Path | None) -> Path | None:
    if explicit:
        return explicit.resolve()
    candidates: list[Path] = []
    if video:
        candidates.extend(sorted(output_dir.glob(f"{video.stem}_frames_*.csv"), reverse=True))
        candidates.extend(sorted(video.resolve().parent.glob(f"{video.stem}_frames_*.csv"), reverse=True))
        candidates.extend(sorted(video.resolve().parent.glob(f"{video.stem}_video_metadata_*\\{video.stem}_frames_*.csv"), reverse=True))
    candidates.extend(sorted(output_dir.glob("*_frames_*.csv"), reverse=True))
    for p in candidates:
        if p.exists():
            return p
    return None


def load_frame_times(path: Path | None) -> dict[int, float]:
    if not path or not path.exists():
        return {}
    out: dict[int, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[int(row["frame_index"])] = float(row["pts_time"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def load_images(frames_dir: Path) -> tuple[dict[int, np.ndarray], list[int]]:
    images: dict[int, np.ndarray] = {}
    for path in sorted(frames_dir.glob("frame_*.png")):
        m = re.search(r"frame_(\d+)\.png$", path.name)
        if not m:
            continue
        idx = int(m.group(1))
        img = imread(path)
        if img is not None:
            images[idx] = img
    indices = sorted(images)
    if len(indices) < 2:
        raise SystemExit(f"Need at least 2 frame PNG files in {frames_dir}")
    return images, indices


def road_polygon_mask(shape: tuple[int, int, int]) -> np.ndarray:
    h, w = shape[:2]
    poly = np.array(
        [
            [int(w * 0.13), h - 1],
            [int(w * 0.76), h - 1],
            [int(w * 0.70), int(h * 0.70)],
            [int(w * 0.59), int(h * 0.60)],
            [int(w * 0.40), int(h * 0.60)],
            [int(w * 0.23), int(h * 0.78)],
        ],
        dtype=np.int32,
    )
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [poly], 255)
    # Common dashcam timestamp/reflection area.
    mask[int(h * 0.86) : h, int(w * 0.60) : w] = 0
    return mask


def marking_mask(img: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = roi
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hch, sch, vch = cv2.split(hsv)
    base = np.zeros(gray.shape, dtype=np.uint8)
    base[y1:y2, x1:x2] = 255
    base = cv2.bitwise_and(base, road_polygon_mask(img.shape))
    white = ((vch > 105) & (sch < 95)).astype(np.uint8) * 255
    yellow = ((vch > 95) & (sch > 45) & (hch >= 12) & (hch <= 45)).astype(np.uint8) * 255
    bright = cv2.bitwise_and(cv2.bitwise_or(white, yellow), base)
    edges = cv2.Canny(gray, 45, 130)
    mask = cv2.bitwise_and(
        cv2.dilate(bright, np.ones((7, 7), np.uint8)),
        cv2.dilate(edges, np.ones((3, 3), np.uint8)),
    )
    if cv2.countNonZero(mask) < 50:
        mask = cv2.dilate(bright, np.ones((3, 3), np.uint8))
    return mask


def generic_mask(img: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = roi
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[max(0, y1) : min(h, y2), max(0, x1) : min(w, x2)] = 255
    # Avoid common bottom-right overlays.
    mask[int(h * 0.86) : h, int(w * 0.60) : w] = 0
    return mask


def dynamic_generic_mask(src: np.ndarray, dst: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    base = generic_mask(src, roi)
    ga = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(dst, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(ga, gb)
    # Focus on changing objects inside the ROI, suppressing static fences/background.
    changed = (diff > 12).astype(np.uint8) * 255
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    changed = cv2.dilate(changed, np.ones((9, 9), np.uint8), iterations=1)
    mask = cv2.bitwise_and(base, changed)
    if cv2.countNonZero(mask) < 80:
        return base
    return mask


def lk_track(src: np.ndarray, dst: np.ndarray, mask: np.ndarray) -> tuple[dict[str, float], np.ndarray, np.ndarray] | None:
    ga = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(dst, cv2.COLOR_BGR2GRAY)
    pts = cv2.goodFeaturesToTrack(ga, maxCorners=700, qualityLevel=0.006, minDistance=6, mask=mask, blockSize=5)
    if pts is None or len(pts) < 8:
        return None
    p2, st, _ = cv2.calcOpticalFlowPyrLK(
        ga,
        gb,
        pts,
        None,
        winSize=(25, 25),
        maxLevel=4,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 45, 0.01),
    )
    good = st.reshape(-1) == 1
    p1 = pts.reshape(-1, 2)[good]
    p2 = p2.reshape(-1, 2)[good]
    if len(p1) < 8:
        return None
    p1_back, st_back, _ = cv2.calcOpticalFlowPyrLK(
        gb,
        ga,
        p2.reshape(-1, 1, 2),
        None,
        winSize=(25, 25),
        maxLevel=4,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 45, 0.01),
    )
    fb = np.linalg.norm(p1_back.reshape(-1, 2) - p1, axis=1)
    ok = (st_back.reshape(-1) == 1) & (fb < 1.5)
    flow = (p2 - p1)[ok]
    if len(flow) < 8:
        return None
    mag = np.linalg.norm(flow, axis=1)
    flow = flow[mag < 80]
    p1 = p1[ok][mag < 80]
    p2 = p2[ok][mag < 80]
    mag = mag[mag < 80]
    if len(flow) < 8:
        return None
    dx = flow[:, 0]
    dy = flow[:, 1]
    stats = {
        "n": float(len(flow)),
        "dx_med": float(np.median(dx)),
        "dy_med": float(np.median(dy)),
        "dy_mean": float(np.mean(dy)),
        "dy_p25": float(np.percentile(dy, 25)),
        "dy_p75": float(np.percentile(dy, 75)),
        "mag_med": float(np.median(mag)),
        "mag_p75": float(np.percentile(mag, 75)),
        "up_pct": float(np.mean(dy < -0.75) * 100),
        "down_pct": float(np.mean(dy > 0.75) * 100),
        "zero_pct": float(np.mean(np.abs(dy) <= 0.75) * 100),
    }
    return stats, p1, p2


def lk_stats(src: np.ndarray, dst: np.ndarray, mask: np.ndarray) -> dict[str, float] | None:
    result = lk_track(src, dst, mask)
    return None if result is None else result[0]


def draw_vector_panel(
    out_path: Path,
    images: dict[int, np.ndarray],
    pair_frames: list[tuple[int, int]],
    roi: Roi,
    title: str,
) -> None:
    panels: list[np.ndarray] = []
    for a, b in pair_frames:
        src = images[a]
        dst = images[b]
        mask = marking_mask(src, roi.box) if roi.kind == "marking" else dynamic_generic_mask(src, dst, roi.box)
        result = lk_track(src, dst, mask)
        x1, y1, x2, y2 = roi.box
        pad = 30
        cx1 = max(0, x1 - pad)
        cy1 = max(0, y1 - pad)
        cx2 = min(src.shape[1], x2 + pad)
        cy2 = min(src.shape[0], y2 + pad)
        crop = src[cy1:cy2, cx1:cx2].copy()
        if result is not None:
            stats, p1, p2 = result
            flow = p2 - p1
            order = np.argsort(np.linalg.norm(flow, axis=1))[::-1][:160]
            for idx in order:
                x, y = p1[idx]
                u, v = flow[idx]
                color = (0, 0, 255) if v < -0.75 else ((0, 255, 0) if v > 0.75 else (0, 255, 255))
                cv2.arrowedLine(
                    crop,
                    (int(round(x - cx1)), int(round(y - cy1))),
                    (int(round(x + u - cx1)), int(round(y + v - cy1))),
                    color,
                    2,
                    tipLength=0.35,
                )
            text = (
                f"{a}->{b} n={int(stats['n'])} "
                f"up={stats['up_pct']:.0f}% down={stats['down_pct']:.0f}% "
                f"mag75={stats['mag_p75']:.1f}px"
            )
        else:
            text = f"{a}->{b} insufficient tracked points"
        cv2.putText(crop, text, (14, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(crop, text, (14, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 1, cv2.LINE_AA)
        panels.append(crop)
    if not panels:
        return
    max_h = max(p.shape[0] for p in panels)
    norm: list[np.ndarray] = []
    for p in panels:
        if p.shape[0] != max_h:
            scale = max_h / p.shape[0]
            p = cv2.resize(p, (int(round(p.shape[1] * scale)), max_h), interpolation=cv2.INTER_AREA)
        norm.append(p)
    panel = np.hstack(norm)
    cv2.putText(panel, title, (16, max_h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(panel, title, (16, max_h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (0, 0, 0), 1, cv2.LINE_AA)
    imwrite(out_path, panel)


def default_rois(mode: str, shape: tuple[int, int, int], case_roi: Roi | None, custom: list[Roi]) -> list[Roi]:
    h, w = shape[:2]
    if mode == "dashcam":
        rois = [
            Roi("all_road_markings", "marking", (int(w * 0.15), int(h * 0.60), int(w * 0.74), h - 1)),
            Roi("lower_center_road_markings", "marking", (int(w * 0.44), int(h * 0.74), int(w * 0.66), int(h * 0.88))),
            Roi("lower_left_guide_line", "marking", (int(w * 0.18), int(h * 0.76), int(w * 0.50), h - 1)),
        ]
    else:
        main = case_roi if case_roi else Roi("accident_vehicle_or_main_roi", "generic", (int(w * 0.20), int(h * 0.20), int(w * 0.85), int(h * 0.90)))
        rois = [
            main,
            Roi("scene_center", "generic", (int(w * 0.10), int(h * 0.10), int(w * 0.90), int(h * 0.90))),
        ]
    rois.extend(custom)
    return rois


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def valid_mb_tokens(s: str) -> list[str]:
    return [p for p in s.split() if p in MB_TOKENS]


def parse_mb_debug(text: str) -> list[dict]:
    new_re = re.compile(r"New frame, type: ([IPB])")
    row_re = re.compile(r"^\[h264 @ [^\]]+\]\s+(\d+)\s+(.*)$")
    frames: list[dict] = []
    cur: dict | None = None
    row_tokens: list[str] | None = None

    def finish_row() -> None:
        nonlocal row_tokens, cur
        if cur is not None and row_tokens is not None:
            cur["rows"].append(row_tokens)
        row_tokens = None

    def finish_frame() -> None:
        nonlocal cur, row_tokens
        if cur is not None:
            finish_row()
            frames.append(cur)
        cur = None
        row_tokens = None

    for line in text.splitlines():
        m = new_re.search(line)
        if m:
            finish_frame()
            cur = {"type": m.group(1), "rows": []}
            continue
        if cur is None:
            continue
        if "nal_unit_type:" in line:
            finish_row()
            continue
        m = row_re.match(line)
        if m:
            tokens = valid_mb_tokens(m.group(2))
            if tokens:
                finish_row()
                row_tokens = tokens[:]
            continue
        if row_tokens is not None:
            tokens = valid_mb_tokens(re.sub(r"^\[h264 @ [^\]]+\]\s*", "", line))
            if tokens:
                row_tokens.extend(tokens)
    finish_frame()
    return frames


def run_mb_stats(video: Path, start: float, duration: float, indices: list[int], output_dir: Path, keep_log: bool) -> tuple[list[dict], Path | None]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "debug",
        "-threads",
        "1",
        "-debug",
        "mb_type",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(video),
        "-an",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    text = proc.stderr.decode("utf-8", errors="replace")
    log_path = None
    if keep_log:
        log_path = output_dir / f"{video.stem}_mb_type_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_path.write_text(text, encoding="utf-8")
    if proc.returncode != 0 and "New frame" not in text:
        return [], log_path
    raw = parse_mb_debug(text)
    if len(raw) > len(indices):
        raw = raw[1 : 1 + len(indices)]
    else:
        raw = raw[: len(indices)]
    rows: list[dict] = []
    for idx, rec in zip(indices, raw):
        tokens = [t for row in rec["rows"] for t in row]
        c = Counter(tokens)
        total = sum(c.values())
        intra = c["I"] + c["i"]
        motion = c[">"] + c[">-"] + c[">|"] + c[">+"]
        rows.append(
            {
                "frame_index": idx,
                "type": rec["type"],
                "total_mb": total,
                "intra_mb": intra,
                "intra_pct": (intra / total * 100) if total else "",
                "skip_S": c["S"],
                "motion_pred": motion,
                "mb_intra_upper": c["I"],
                "mb_intra_lower": c["i"],
            }
        )
    return rows, log_path


def draw_timeline(path: Path, motion_rows: list[dict], mb_rows: list[dict], primary_roi: str) -> None:
    w, h = 1500, 680
    ml, mr, mt, mb = 90, 40, 50, 90
    pw, ph = w - ml - mr, h - mt - mb
    img = np.full((h, w, 3), 255, dtype=np.uint8)

    frames = [int(r["to_frame"]) for r in motion_rows]
    if mb_rows:
        frames.extend(int(r["frame_index"]) for r in mb_rows)
    fmin, fmax = min(frames), max(frames)

    def x_for(frame: int) -> int:
        return int(ml + (frame - fmin) / max(1, fmax - fmin) * pw)

    def y_for(pct: float) -> int:
        return int(mt + (100 - pct) / 100 * ph)

    for pct in [0, 20, 40, 60, 80, 100]:
        y = y_for(pct)
        cv2.line(img, (ml, y), (w - mr, y), (225, 225, 225), 1)
        cv2.putText(img, str(pct), (35, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 80, 80), 1, cv2.LINE_AA)

    cv2.rectangle(img, (ml, mt), (w - mr, h - mb), (90, 90, 90), 1)
    cv2.putText(img, "Selected interval frame analysis", (ml, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA)

    def draw(points: list[tuple[int, float]], color: tuple[int, int, int], label: str, yoff: int) -> None:
        pts = [(x_for(fr), y_for(p)) for fr, p in points if p != ""]
        for a, b in zip(pts, pts[1:]):
            cv2.line(img, a, b, color, 2, cv2.LINE_AA)
        for x, y in pts:
            cv2.circle(img, (x, y), 2, color, -1)
        cv2.line(img, (ml + 10, yoff), (ml + 45, yoff), color, 3, cv2.LINE_AA)
        cv2.putText(img, label, (ml + 55, yoff + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 0, 0), 1, cv2.LINE_AA)

    draw([(int(r["to_frame"]), float(r.get(f"{primary_roi}_up_pct", 0) or 0)) for r in motion_rows], (0, 150, 0), f"{primary_roi} upward-vector %", h - 58)
    draw([(int(r["to_frame"]), float(r.get(f"{primary_roi}_mag_p75", 0) or 0)) for r in motion_rows], (180, 80, 0), f"{primary_roi} p75 motion px", h - 34)
    if mb_rows:
        draw([(int(r["frame_index"]), float(r["intra_pct"] or 0)) for r in mb_rows], (0, 0, 210), "intra MB %", h - 10)

    imwrite(path, img)


def write_summary(
    path: Path,
    mode: str,
    video: Path | None,
    frames_dir: Path,
    indices: list[int],
    frame_times: dict[int, float],
    rois: list[Roi],
    motion_rows: list[dict],
    mb_rows: list[dict],
    outputs: dict[str, Path | None],
) -> None:
    primary = rois[0].name
    lines: list[str] = []
    lines.append("视频选定区间帧画面分析说明")
    lines.append("=" * 60)
    if video:
        lines.append(f"视频文件: {video}")
    lines.append(f"帧目录: {frames_dir}")
    lines.append(f"分析模式: {mode}")
    lines.append(f"帧范围: {indices[0]} - {indices[-1]}，共 {len(indices)} 帧")
    if frame_times:
        t0 = frame_times.get(indices[0], "")
        t1 = frame_times.get(indices[-1], "")
        lines.append(f"时间范围: {t0} - {t1}")
    lines.append("")
    lines.append("分析重点:")
    if mode == "dashcam":
        lines.append("- 行车记录仪画面优先关注路面标线、分道线、导向箭头等路面参照物。")
        lines.append("- 若用于测速，物理 delta s 本身可能可靠，但目标通过参照线的帧判定需要单独评估。")
    else:
        lines.append("- 监控画面优先关注事故车辆或用户指定 ROI 的运动变化。")
        lines.append("- 若选定区间未覆盖事故发生过程，应请用户重新反馈时间段或事故车辆 ROI。")
    lines.append("")
    lines.append("ROI:")
    for roi in rois:
        lines.append(f"- {roi.name}: {roi.kind}, {roi.box}")
    lines.append("")

    lines.append("运动异常候选（按主 ROI 向上矢量比例排序，图像坐标 dy<0 为向上）:")
    top_up = sorted(
        [r for r in motion_rows if r.get(f"{primary}_up_pct") != ""],
        key=lambda r: float(r.get(f"{primary}_up_pct", 0) or 0),
        reverse=True,
    )[:10]
    for r in top_up:
        lines.append(
            f"- {r['pair']}: up={float(r[f'{primary}_up_pct']):.1f}%, "
            f"down={float(r[f'{primary}_down_pct']):.1f}%, "
            f"mag_p75={float(r[f'{primary}_mag_p75']):.2f}px"
        )
    lines.append("")

    if mb_rows:
        lines.append("P/I 帧宏块类型异常候选（按 intra 宏块比例排序）:")
        for r in sorted(mb_rows, key=lambda x: float(x["intra_pct"] or 0), reverse=True)[:10]:
            lines.append(
                f"- frame {r['frame_index']} type={r['type']}: "
                f"intra={float(r['intra_pct']):.1f}% ({r['intra_mb']}/{r['total_mb']})"
            )
        lines.append("")

    lines.append("解释边界:")
    lines.append("- PTS/DTS 连续只能说明时间轴未见明显断裂，不能单独证明局部画面几何关系稳定。")
    lines.append("- 编码运动矢量和光流/特征点运动不是同一概念；测速应优先关注实际画面参照物的稳定性。")
    lines.append("- 若只跨少数帧测速，应给出通过帧判定不确定度，例如 +/-1 帧或 +/-2 帧。")
    lines.append("- 矢量图适合用于可视化说明；量化 CSV 是复核和报告引用的基础数据，应同步保留。")
    lines.append("")
    lines.append("输出文件:")
    for name, out in outputs.items():
        if out:
            lines.append(f"- {name}: {out}")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze selected interval frame images for visual instability.")
    parser.add_argument("--video", type=Path, default=None, help="Source video path, used for optional MB-type analysis.")
    parser.add_argument("--frames-dir", type=Path, required=True, help="Directory containing frame_*.png files.")
    parser.add_argument("--frames-csv", type=Path, default=None, help="Frame CSV from extract.ps1.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory. Defaults to a timestamped directory.")
    parser.add_argument("--mode", choices=["dashcam", "surveillance"], required=True, help="Analysis focus.")
    parser.add_argument("--case-roi", type=parse_roi, default=None, help="Main accident vehicle ROI for surveillance mode.")
    parser.add_argument("--roi", type=parse_roi, action="append", default=[], help="Additional ROI. Repeatable.")
    parser.add_argument("--start-time", type=float, default=None, help="Selected interval start time for MB analysis.")
    parser.add_argument("--duration", type=float, default=None, help="Selected interval duration for MB analysis.")
    parser.add_argument("--no-mb", action="store_true", help="Skip H.264 macroblock type analysis.")
    parser.add_argument("--keep-mb-log", action="store_true", help="Keep raw ffmpeg -debug mb_type log.")
    parser.add_argument("--max-vector-pairs", type=int, default=4, help="Number of top abnormal pairs to visualize.")
    args = parser.parse_args()

    video = args.video.resolve() if args.video else None
    frames_dir = args.frames_dir.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else default_output_dir(video, frames_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    images, indices = load_images(frames_dir)
    frame_csv = find_frame_csv(output_dir, video, args.frames_csv)
    frame_times = load_frame_times(frame_csv)
    rois = default_rois(args.mode, images[indices[0]].shape, args.case_roi, args.roi)

    motion_rows: list[dict] = []
    for a, b in zip(indices, indices[1:]):
        row: dict = {
            "pair": f"{a}->{b}",
            "from_frame": a,
            "to_frame": b,
            "from_time": frame_times.get(a, ""),
            "to_time": frame_times.get(b, ""),
        }
        for roi in rois:
            mask = marking_mask(images[a], roi.box) if roi.kind == "marking" else dynamic_generic_mask(images[a], images[b], roi.box)
            stats = lk_stats(images[a], images[b], mask)
            keys = ["n", "dx_med", "dy_med", "dy_mean", "dy_p25", "dy_p75", "mag_med", "mag_p75", "up_pct", "down_pct", "zero_pct"]
            for key in keys:
                row[f"{roi.name}_{key}"] = "" if stats is None else stats[key]
        motion_rows.append(row)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Keep derived filenames short; case/video directories are often already long.
    stem = "video" if video else "frames"
    motion_csv = output_dir / f"{stem}_selected_frame_motion_{ts}.csv"
    write_csv(motion_csv, motion_rows)

    mb_rows: list[dict] = []
    mb_csv: Path | None = None
    mb_log: Path | None = None
    if video and not args.no_mb and args.start_time is not None and args.duration is not None:
        mb_rows, mb_log = run_mb_stats(video, args.start_time, args.duration, indices, output_dir, args.keep_mb_log)
        if mb_rows:
            mb_csv = output_dir / f"{stem}_selected_mb_types_{ts}.csv"
            write_csv(mb_csv, mb_rows)

    timeline_png = output_dir / f"{stem}_selected_interval_timeline_{ts}.png"
    draw_timeline(timeline_png, motion_rows, mb_rows, rois[0].name)

    primary_roi = rois[0]
    top_pair_rows = sorted(
        [r for r in motion_rows if r.get(f"{primary_roi.name}_up_pct") != ""],
        key=lambda r: float(r.get(f"{primary_roi.name}_up_pct", 0) or 0),
        reverse=True,
    )[: max(1, args.max_vector_pairs)]
    vector_png = output_dir / f"{stem}_{primary_roi.name}_vectors_{ts}.png"
    draw_vector_panel(
        vector_png,
        images,
        [(int(r["from_frame"]), int(r["to_frame"])) for r in top_pair_rows],
        primary_roi,
        "red=up, green=down, yellow=near-zero; vector panel for top abnormal pairs",
    )

    summary_txt = output_dir / f"{stem}_analysis_summary_{ts}.txt"
    outputs = {
        "motion_csv": motion_csv,
        "mb_csv": mb_csv,
        "timeline_png": timeline_png,
        "vector_png": vector_png,
        "summary_txt": summary_txt,
        "mb_debug_log": mb_log,
    }
    write_summary(summary_txt, args.mode, video, frames_dir, indices, frame_times, rois, motion_rows, mb_rows, outputs)

    print(f"motion_csv={motion_csv}")
    if mb_csv:
        print(f"mb_csv={mb_csv}")
    print(f"timeline_png={timeline_png}")
    print(f"vector_png={vector_png}")
    print(f"summary_txt={summary_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
