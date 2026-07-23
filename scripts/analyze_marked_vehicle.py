#!/usr/bin/env python3
"""Analyze a user-marked vehicle for PTS/visual-motion consistency.

Inputs:
- decoded frame_*.png files from extract.ps1;
- frame CSV from extract.ps1;
- a user-provided vehicle box on one anchor frame.

Outputs:
- per-frame tracking CSV;
- representative/problem-frame speed-vector review image;
- four-part Chinese analysis note.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class TrackPoint:
    frame_index: int
    cx: float
    cy: float
    n_points: int


def imread(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise RuntimeError(f"Failed to encode image: {path}")
    buf.tofile(str(path))


def frame_index(path: Path) -> int:
    m = re.search(r"frame_(\d+)\.png$", path.name)
    if not m:
        raise ValueError(path.name)
    return int(m.group(1))


def parse_box(value: str) -> tuple[int, int, int, int]:
    nums = [int(v) for v in value.split(",")]
    if len(nums) != 4:
        raise argparse.ArgumentTypeError("box must be x1,y1,x2,y2")
    x1, y1, x2, y2 = nums
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError("box x2/y2 must be greater than x1/y1")
    return x1, y1, x2, y2


def load_frame_times(path: Path, first_pts_time: float | None) -> tuple[dict[int, dict], float]:
    rows: dict[int, dict] = {}
    first_pts = first_pts_time
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            idx = int(row["frame_index"])
            pts = float(row["pts_time"])
            if first_pts is None:
                first_pts = pts
            rows[idx] = {
                "pts_time": pts,
                "duration_time": float(row.get("duration_time") or 0),
                "pkt_pos": row.get("pkt_pos", ""),
                "pkt_size": row.get("pkt_size", ""),
                "pict_type": row.get("pict_type", ""),
                "key_frame": row.get("key_frame", ""),
            }
    if first_pts is None:
        raise SystemExit(f"No frame rows in {path}")
    for row in rows.values():
        row["rel_pts_time"] = row["pts_time"] - first_pts
    return rows, first_pts


def good_points(gray: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    h, w = gray.shape[:2]
    x1, y1, x2, y2 = box
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    mask = np.zeros_like(gray)
    mask[y1:y2, x1:x2] = 255
    pts = cv2.goodFeaturesToTrack(
        gray,
        maxCorners=160,
        qualityLevel=0.01,
        minDistance=4,
        blockSize=5,
        mask=mask,
    )
    if pts is None:
        return np.empty((0, 1, 2), dtype=np.float32)
    return pts.astype(np.float32)


def robust_median_flow(src_gray: np.ndarray, dst_gray: np.ndarray, points: np.ndarray) -> tuple[np.ndarray | None, np.ndarray]:
    dst_points, status, _ = cv2.calcOpticalFlowPyrLK(
        src_gray,
        dst_gray,
        points,
        None,
        winSize=(25, 25),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if dst_points is None or status is None:
        return None, np.empty((0, 1, 2), dtype=np.float32)
    src_good = points[status.ravel() == 1].reshape(-1, 2)
    dst_good = dst_points[status.ravel() == 1].reshape(-1, 2)
    if len(dst_good) < 8:
        return None, dst_good.reshape(-1, 1, 2).astype(np.float32)
    flow = dst_good - src_good
    med = np.median(flow, axis=0)
    residual = np.linalg.norm(flow - med, axis=1)
    keep = residual <= max(3.0, np.percentile(residual, 70))
    if keep.sum() >= 8:
        flow = flow[keep]
        dst_good = dst_good[keep]
        med = np.median(flow, axis=0)
    return med.astype(np.float32), dst_good.reshape(-1, 1, 2).astype(np.float32)


def track_direction(
    images: dict[int, np.ndarray],
    ordered_indices: list[int],
    anchor_frame: int,
    anchor_box: tuple[int, int, int, int],
) -> dict[int, TrackPoint]:
    anchor_img = images[anchor_frame]
    gray = cv2.cvtColor(anchor_img, cv2.COLOR_BGR2GRAY)
    x1, y1, x2, y2 = anchor_box
    center = np.array([(x1 + x2) / 2.0, (y1 + y2) / 2.0], dtype=np.float32)
    points = good_points(gray, anchor_box)
    out = {anchor_frame: TrackPoint(anchor_frame, float(center[0]), float(center[1]), int(len(points)))}

    curr_gray = gray
    curr_center = center
    curr_points = points
    for idx in ordered_indices:
        if len(curr_points) < 12:
            half_w = max(55, int((x2 - x1) / 2))
            half_h = max(30, int((y2 - y1) / 2))
            refresh_box = (
                int(curr_center[0] - half_w),
                int(curr_center[1] - half_h),
                int(curr_center[0] + half_w),
                int(curr_center[1] + half_h),
            )
            curr_points = good_points(curr_gray, refresh_box)
            if len(curr_points) < 8:
                break
        next_gray = cv2.cvtColor(images[idx], cv2.COLOR_BGR2GRAY)
        med, next_points = robust_median_flow(curr_gray, next_gray, curr_points)
        if med is None:
            break
        curr_center = curr_center + med
        out[idx] = TrackPoint(idx, float(curr_center[0]), float(curr_center[1]), int(len(next_points)))
        curr_gray = next_gray
        curr_points = next_points
    return out


def build_rows(points: dict[int, TrackPoint], frame_times: dict[int, dict], first_pts: float) -> list[dict]:
    rows: list[dict] = []
    prev: dict | None = None
    for idx in sorted(points):
        p = points[idx]
        meta = frame_times.get(idx, {})
        row = {
            "frame_index": idx,
            "pts_time": meta.get("pts_time", ""),
            "rel_pts_time": meta.get("rel_pts_time", ""),
            "duration_time": meta.get("duration_time", ""),
            "cx": p.cx,
            "cy": p.cy,
            "n_points": p.n_points,
            "dt": "",
            "dx": "",
            "dy": "",
            "pixel_displacement": "",
            "pixel_speed": "",
            "speed_ratio_to_median": "",
            "direction_deg": "",
            "anomaly": "",
            "pict_type": meta.get("pict_type", ""),
            "key_frame": meta.get("key_frame", ""),
            "pkt_pos": meta.get("pkt_pos", ""),
            "pkt_size": meta.get("pkt_size", ""),
        }
        if prev is not None and row["rel_pts_time"] != "" and prev["rel_pts_time"] != "":
            dt = float(row["rel_pts_time"]) - float(prev["rel_pts_time"])
            dx = row["cx"] - float(prev["cx"])
            dy = row["cy"] - float(prev["cy"])
            disp = math.hypot(dx, dy)
            row["dt"] = dt
            row["dx"] = dx
            row["dy"] = dy
            row["pixel_displacement"] = disp
            row["pixel_speed"] = disp / dt if dt else ""
            row["direction_deg"] = math.degrees(math.atan2(dy, dx)) if disp else ""
        rows.append(row)
        prev = row

    speeds = [float(r["pixel_speed"]) for r in rows if r["pixel_speed"] != ""]
    median = float(np.median(speeds)) if speeds else 0.0
    for r in rows:
        if r["pixel_speed"] == "" or median <= 0:
            continue
        ratio = float(r["pixel_speed"]) / median
        r["speed_ratio_to_median"] = ratio
        if ratio >= 1.8:
            r["anomaly"] = "pixel_speed_near_2x"
        elif ratio <= 0.2:
            r["anomaly"] = "pixel_speed_near_zero"
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "frame_index",
        "pts_time",
        "rel_pts_time",
        "duration_time",
        "cx",
        "cy",
        "n_points",
        "dt",
        "dx",
        "dy",
        "pixel_displacement",
        "pixel_speed",
        "speed_ratio_to_median",
        "direction_deg",
        "anomaly",
        "pict_type",
        "key_frame",
        "pkt_pos",
        "pkt_size",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def select_visual_rows(rows: list[dict]) -> list[dict]:
    """Pick representative frames plus problem frames for human review."""
    if not rows:
        return []
    by_idx = {int(r["frame_index"]): r for r in rows}
    frame_indices = sorted(by_idx)
    pick_indices: set[int] = set()
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        pos = round((len(frame_indices) - 1) * fraction)
        pick_indices.add(frame_indices[pos])
    anomaly_indices = {int(r["frame_index"]) for r in rows if r.get("anomaly")}
    for idx in anomaly_indices:
        pick_indices.add(idx)
        if idx - 1 in by_idx:
            pick_indices.add(idx - 1)
        if idx + 1 in by_idx:
            pick_indices.add(idx + 1)
    return [by_idx[i] for i in sorted(pick_indices)]


def reference_points(gray: np.ndarray, cx: float, cy: float, base_w: int, base_h: int) -> np.ndarray:
    h, w = gray.shape[:2]
    half_w = max(55, int(base_w * 0.42))
    half_h = max(42, int(base_h * 0.30))
    x1 = max(0, int(cx - half_w))
    y1 = max(0, int(cy - half_h))
    x2 = min(w, int(cx + half_w))
    y2 = min(h, int(cy + half_h))
    if x2 <= x1 or y2 <= y1:
        return np.empty((0, 1, 2), dtype=np.float32)
    mask = np.zeros_like(gray)
    cv2.ellipse(mask, (int(cx), int(cy)), (half_w, half_h), 0, 0, 360, 255, -1)
    pts = cv2.goodFeaturesToTrack(
        gray,
        maxCorners=24,
        qualityLevel=0.02,
        minDistance=12,
        blockSize=5,
        mask=mask,
    )
    if pts is None:
        return np.empty((0, 1, 2), dtype=np.float32)
    candidates = pts.astype(np.float32)
    flat = candidates.reshape(-1, 2)
    order = np.argsort((flat[:, 0] - cx) ** 2 + (flat[:, 1] - cy) ** 2)
    return candidates[order[:12]].reshape(-1, 1, 2)


def tracked_reference_vectors(
    prev_img: np.ndarray,
    curr_img: np.ndarray,
    prev_row: dict,
    base_w: int,
    base_h: int,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    prev_gray = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY)
    curr_gray = cv2.cvtColor(curr_img, cv2.COLOR_BGR2GRAY)
    prev_cx, prev_cy = float(prev_row["cx"]), float(prev_row["cy"])
    pts = reference_points(prev_gray, prev_cx, prev_cy, base_w, base_h)
    if len(pts) == 0:
        return []
    dst, status, _ = cv2.calcOpticalFlowPyrLK(
        prev_gray,
        curr_gray,
        pts,
        None,
        winSize=(25, 25),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if dst is None or status is None:
        return []
    src_good = pts[status.ravel() == 1].reshape(-1, 2)
    dst_good = dst[status.ravel() == 1].reshape(-1, 2)
    if len(dst_good) == 0:
        return []
    flow = dst_good - src_good
    med = np.median(flow, axis=0)
    residual = np.linalg.norm(flow - med, axis=1)
    keep = residual <= max(3.0, np.percentile(residual, 70))
    src_good = src_good[keep]
    dst_good = dst_good[keep]
    if len(dst_good) == 0:
        return []
    order = np.argsort(np.linalg.norm(src_good - np.array([prev_cx, prev_cy], dtype=np.float32), axis=1))
    vectors = []
    for i in order[:8]:
        start = (int(round(src_good[i][0])), int(round(src_good[i][1])))
        end = (int(round(dst_good[i][0])), int(round(dst_good[i][1])))
        vectors.append((start, end))
    return vectors


def draw_sheet(
    path: Path,
    images: dict[int, np.ndarray],
    rows: list[dict],
    crop_box: tuple[int, int, int, int] | None,
    anchor_box: tuple[int, int, int, int],
) -> None:
    tiles = []
    selected_rows = select_visual_rows(rows)
    row_by_idx = {int(r["frame_index"]): r for r in rows}
    base_w = anchor_box[2] - anchor_box[0]
    base_h = anchor_box[3] - anchor_box[1]
    anomaly_indices = {int(r["frame_index"]) for r in rows if r.get("anomaly")}

    for r in selected_rows:
        idx = int(r["frame_index"])
        img = images[idx].copy()
        cx, cy = float(r["cx"]), float(r["cy"])
        color = (0, 0, 255) if idx in anomaly_indices else (0, 200, 255)
        roi_w = max(80, int(base_w * 0.55))
        roi_h = max(55, int(base_h * 0.35))
        cv2.rectangle(img, (round(cx - roi_w), round(cy - roi_h)), (round(cx + roi_w), round(cy + roi_h)), color, 2)

        if r["dx"] != "" and r["dy"] != "":
            prev = (round(cx - float(r["dx"])), round(cy - float(r["dy"])))
            curr = (round(cx), round(cy))
            cv2.arrowedLine(img, prev, curr, (0, 0, 255), 4, tipLength=0.35)
            cv2.putText(img, "V", (curr[0] + 8, curr[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        cv2.circle(img, (round(cx), round(cy)), 7, (0, 0, 255), -1)
        cv2.putText(img, "C", (round(cx) + 8, round(cy) + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        prev_idx = idx - 1
        if prev_idx in images and prev_idx in row_by_idx:
            vectors = tracked_reference_vectors(images[prev_idx], images[idx], row_by_idx[prev_idx], base_w, base_h)
            for n, (start, end) in enumerate(vectors, start=1):
                cv2.arrowedLine(img, start, end, (255, 0, 255), 2, tipLength=0.45)
                cv2.circle(img, start, 3, (255, 0, 255), -1)
                cv2.circle(img, end, 4, (255, 0, 255), -1)
                cv2.putText(img, f"T{n}", (end[0] + 5, end[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)

        if crop_box:
            x1, y1, x2, y2 = crop_box
            tile = img[y1:y2, x1:x2]
        else:
            h, w = img.shape[:2]
            x1, y1 = max(0, round(cx - 440)), max(0, round(cy - 120))
            x2, y2 = min(w, round(cx + 440)), min(h, round(cy + 120))
            tile = img[y1:y2, x1:x2]
        if tile.size == 0:
            continue
        rel_text = f"frame {idx}  rel={float(r['rel_pts_time']):.2f}s"
        speed_text = "speed=N/A" if r["pixel_speed"] == "" else f"speed={float(r['pixel_speed']):.1f}px/s"
        cv2.rectangle(tile, (8, 8), (390, 84 if idx not in anomaly_indices else 116), (0, 0, 0), -1)
        cv2.putText(tile, rel_text, (18, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
        cv2.putText(tile, speed_text, (18, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
        if idx in anomaly_indices:
            cv2.putText(tile, f"CHECK: {r['anomaly']}", (18, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.rectangle(tile, (8, tile.shape[0] - 43), (520, tile.shape[0] - 8), (0, 0, 0), -1)
        cv2.putText(tile, "C=center vector  T=tracked point vector, previous frame to current frame", (18, tile.shape[0] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        tile = cv2.resize(tile, (760, 430), interpolation=cv2.INTER_AREA)
        tiles.append(tile)
    if not tiles:
        return
    cols = 2
    blank = np.zeros_like(tiles[0])
    rows_img = []
    for i in range(0, len(tiles), cols):
        row = tiles[i : i + cols]
        row += [blank.copy()] * (cols - len(row))
        rows_img.append(np.hstack(row))
    imwrite(path, np.vstack(rows_img))


def stable_rows(rows: list[dict], min_frame: int | None, max_frame: int | None) -> list[dict]:
    out = []
    for r in rows:
        idx = int(r["frame_index"])
        if min_frame is not None and idx < min_frame:
            continue
        if max_frame is not None and idx > max_frame:
            continue
        out.append(r)
    return out


def write_report(
    path: Path,
    rows: list[dict],
    first_pts: float,
    anchor_frame: int,
    anchor_box: tuple[int, int, int, int],
    unstable_note: str,
) -> None:
    usable = [r for r in rows if r["pixel_speed"] != ""]
    if not usable:
        raise SystemExit("No usable pixel-speed rows for report")
    speeds = [float(r["pixel_speed"]) for r in usable]
    median = float(np.median(speeds))
    anomalies = [r for r in usable if r["anomaly"]]
    first, last = rows[0], rows[-1]
    dt_values = [float(r["dt"]) for r in usable if r["dt"] != ""]
    dt_bad = [v for v in dt_values if abs(v - (dt_values[0] if dt_values else v)) > 1e-6]

    samples = []
    for r in rows:
        idx = int(r["frame_index"])
        if r["pixel_speed"] == "":
            continue
        if idx % 5 == 0 or idx in {int(first["frame_index"]), int(last["frame_index"])}:
            samples.append(
                f"  - frame {idx}，rel_pts_time={float(r['rel_pts_time']):.2f}s，"
                f"dx={float(r['dx']):.2f}px，dy={float(r['dy']):.2f}px，"
                f"pixel_speed={float(r['pixel_speed']):.2f}px/s，"
                f"方向角={float(r['direction_deg']):.2f}°"
            )

    anomaly_text = "未检出 pixel_speed 约 2 倍或接近 0 的孤立异常帧。"
    if anomalies:
        parts = []
        for r in anomalies[:20]:
            parts.append(
                f"frame {r['frame_index']} rel={float(r['rel_pts_time']):.2f}s "
                f"speed={float(r['pixel_speed']):.2f}px/s ratio={float(r['speed_ratio_to_median']):.2f} {r['anomaly']}"
            )
        anomaly_text = "检出以下疑似异常帧，需人工逐帧复核：" + "；".join(parts)

    state = "减速"
    if speeds[-1] > speeds[0] * 1.15:
        state = "加速"
    elif speeds[-1] >= speeds[0] * 0.85:
        state = "接近匀速"
    direction = "向画面右侧并略向上" if float(last["cx"]) > float(first["cx"]) and float(last["cy"]) < float(first["cy"]) else "沿图像平面连续"

    x1, y1, x2, y2 = anchor_box
    text = f"""用户标注车辆：时间轴与画面像素速度一致性分析

第一部分：分析时间轴 PTS 数据

1. 分析对象为用户在图片中标注的指定车辆，锚定帧为 frame_index={anchor_frame}，用户标注框换算到原始画面坐标为 x1={x1},y1={y1},x2={x2},y2={y2}。
2. 视频首帧 first_pts_time={first_pts:.6f} s；本说明采用 rel_pts_time = pts_time - first_pts_time 作为相对时间。
3. 本次正式分析区间为 frame_index={first['frame_index']}-{last['frame_index']}，对应 rel_pts_time={float(first['rel_pts_time']):.2f}-{float(last['rel_pts_time']):.2f} s。
4. 该区间帧号连续；相邻帧 delta_pts_time 以 {dt_values[0]:.6f} s 为主，duration_time 与帧率结构匹配。
5. 时间轴异常检查结果：{'未见 PTS 间隔突变。' if not dt_bad else '存在 PTS 间隔突变，需结合 CSV 复核。'}

第二部分：视频画面指定车辆的速度矢量分析

1. 目标车辆依据用户标注确定，禁止自行替换或另选目标车辆。
2. 采用 LK 光流对标注车辆进行逐帧中心点跟踪，计算 dx、dy、pixel_displacement、pixel_speed 和 direction_deg。坐标系为图像坐标：x 向右为正，y 向下为正，dy<0 表示向画面上方移动。
3. 目标中心由 frame {first['frame_index']} 的约 ({float(first['cx']):.2f}, {float(first['cy']):.2f}) 移至 frame {last['frame_index']} 的约 ({float(last['cx']):.2f}, {float(last['cy']):.2f})。
4. 逐帧像素速度中位数约为 {median:.2f} px/s。{anomaly_text}
5. 主要采样帧速度矢量如下：
{chr(10).join(samples)}

第三部分：结合时间轴和视频画面，分析一致性

1. 时间轴方面，选定区间内 rel_pts_time 按帧线性递增。
2. 画面方面，用户标注车辆中心点随帧号增大连续移动，像素速度变化为连续趋势。
3. 核验规则：若时间轴线性递增但某帧 pixel_speed 约为前后稳定值 2 倍，应提示疑似跳帧或 ROI 跟踪异常；若 pixel_speed 接近 0 且目标应持续运动，应提示疑似重复帧或冻结帧。
4. 本区间核验结果：{anomaly_text}

第四部分：结论

1. 时间轴与视频画面一致性：在 frame_index={first['frame_index']}-{last['frame_index']}、rel_pts_time={float(first['rel_pts_time']):.2f}-{float(last['rel_pts_time']):.2f} s 范围内，时间轴与用户标注车辆画面运动{'存在需人工复核的疑似异常。' if anomalies else '具有一致性。'}
2. 指定车辆运动状态：用户标注车辆在该区间内持续{direction}行驶，图像平面像素速度整体表现为{state}趋势。
3. 行驶状态对应时间：该车在 rel_pts_time={float(first['rel_pts_time']):.2f}-{float(last['rel_pts_time']):.2f} s 内像素速度由约 {speeds[0]:.2f} px/s 变化至约 {speeds[-1]:.2f} px/s。
4. 复核边界：像素速度为图像平面速度，不等同于实际物理车速。{unstable_note}
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a user-marked vehicle for PTS/visual consistency.")
    parser.add_argument("--frames-dir", type=Path, required=True, help="Directory containing frame_*.png files.")
    parser.add_argument("--frames-csv", type=Path, required=True, help="Frame CSV from extract.ps1.")
    parser.add_argument("--anchor-frame", type=int, required=True, help="Frame where the user-marked box is defined.")
    parser.add_argument("--box", type=parse_box, required=True, help="User-marked vehicle box x1,y1,x2,y2 in original frame coordinates.")
    parser.add_argument("--start-frame", type=int, required=True, help="First frame to analyze or attempt tracking.")
    parser.add_argument("--end-frame", type=int, required=True, help="Last frame to analyze or attempt tracking.")
    parser.add_argument("--stable-start-frame", type=int, default=None, help="Optional first reliable frame for formal report.")
    parser.add_argument("--stable-end-frame", type=int, default=None, help="Optional last reliable frame for formal report.")
    parser.add_argument("--first-pts-time", type=float, default=None, help="Override first PTS time; defaults to first row in frames CSV.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory.")
    parser.add_argument("--crop", type=parse_box, default=None, help="Optional vector-review crop x1,y1,x2,y2.")
    parser.add_argument("--unstable-note", default="若目标车辆在部分时段被遮挡、与其它车辆重叠或跟踪点漂移，应由用户另行标注该时段目标位置后再分析。")
    args = parser.parse_args()

    frames_dir = args.frames_dir.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else frames_dir.parent / "marked_vehicle_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {frame_index(p): p for p in frames_dir.glob("frame_*.png")}
    lo, hi = min(args.start_frame, args.end_frame), max(args.start_frame, args.end_frame)
    wanted = sorted(i for i in paths if lo <= i <= hi)
    if args.anchor_frame not in wanted:
        raise SystemExit("anchor-frame must be within start/end frame range and exist in frames-dir")
    images = {i: imread(paths[i]) for i in wanted}
    images = {i: img for i, img in images.items() if img is not None}
    frame_times, first_pts = load_frame_times(args.frames_csv.resolve(), args.first_pts_time)

    before = sorted([i for i in images if i < args.anchor_frame], reverse=True)
    after = sorted([i for i in images if i > args.anchor_frame])
    tracked = {}
    tracked.update(track_direction(images, before, args.anchor_frame, args.box))
    tracked.update(track_direction(images, after, args.anchor_frame, args.box))
    tracked[args.anchor_frame] = TrackPoint(args.anchor_frame, (args.box[0] + args.box[2]) / 2, (args.box[1] + args.box[3]) / 2, 0)

    all_rows = build_rows(tracked, frame_times, first_pts)
    csv_path = output_dir / "marked_vehicle_tracking.csv"
    write_csv(csv_path, all_rows)

    report_rows = stable_rows(all_rows, args.stable_start_frame, args.stable_end_frame)
    report_rows = [r for r in report_rows if r["rel_pts_time"] != ""]
    if len(report_rows) < 2:
        raise SystemExit("Not enough stable rows for report")
    report_csv = output_dir / "marked_vehicle_tracking_stable.csv"
    write_csv(report_csv, report_rows)

    sheet_path = output_dir / "marked_vehicle_tracking_sheet.png"
    draw_sheet(sheet_path, images, report_rows, args.crop, args.box)

    report_path = output_dir / "marked_vehicle_four_part_analysis.txt"
    write_report(report_path, report_rows, first_pts, args.anchor_frame, args.box, args.unstable_note)

    print(f"tracking_csv={csv_path}")
    print(f"stable_tracking_csv={report_csv}")
    print(f"tracking_sheet={sheet_path}")
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
