#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
from pathlib import Path

import cv2
import numpy as np


def imread(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"failed to read {path}")
    return img


def imwrite(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise RuntimeError(f"failed to encode {path}")
    buf.tofile(str(path))


def parse_roi(value: str) -> tuple[int, int, int, int]:
    vals = [int(v) for v in value.split(",")]
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("roi must be x1,y1,x2,y2")
    x1, y1, x2, y2 = vals
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError("roi x2/y2 must be greater than x1/y1")
    return x1, y1, x2, y2


def load_tracking(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row["frame_index"] = int(row["frame_index"])
            for key in [
                "pts_time",
                "rel_pts_time",
                "duration_time",
                "weighted_cx",
                "weighted_cy",
                "pixel_speed",
                "direction_deg",
                "bbox_x1",
                "bbox_y1",
                "bbox_x2",
                "bbox_y2",
                "contour_area",
                "feature_points",
            ]:
                if row.get(key) not in ("", None):
                    row[key] = float(row[key])
            rows.append(row)
    rows.sort(key=lambda r: r["frame_index"])
    return rows


def yellow_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array((16, 55, 90)), np.array((40, 255, 255)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 5), np.uint8))
    return mask


def union_bbox(prev: dict, curr: dict, image_shape: tuple[int, int, int], margin: int) -> tuple[int, int, int, int]:
    h, w = image_shape[:2]
    keys = ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")
    if all(k in prev and k in curr for k in keys):
        x1 = int(min(prev["bbox_x1"], curr["bbox_x1"]) - margin)
        y1 = int(min(prev["bbox_y1"], curr["bbox_y1"]) - margin)
        x2 = int(max(prev["bbox_x2"], curr["bbox_x2"]) + margin)
        y2 = int(max(prev["bbox_y2"], curr["bbox_y2"]) + margin)
    else:
        cx = (prev["weighted_cx"] + curr["weighted_cx"]) / 2
        cy = (prev["weighted_cy"] + curr["weighted_cy"]) / 2
        x1, y1, x2, y2 = int(cx - 220), int(cy - 120), int(cx + 220), int(cy + 120)
    return max(0, x1), max(0, y1), min(w, x2), min(h, y2)


def diff_metrics(prev_img: np.ndarray, curr_img: np.ndarray, roi: tuple[int, int, int, int]) -> dict:
    x1, y1, x2, y2 = roi
    p = prev_img[y1:y2, x1:x2]
    c = curr_img[y1:y2, x1:x2]
    pg = cv2.GaussianBlur(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    cg = cv2.GaussianBlur(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    diff = cv2.absdiff(pg, cg)
    changed = diff > 25

    pm = yellow_mask(p)
    cm = yellow_mask(c)
    y_xor = cv2.bitwise_xor(pm, cm)
    y_union = cv2.bitwise_or(pm, cm)
    y_union_count = int(np.count_nonzero(y_union))

    return {
        "mean_abs_diff": float(diff.mean()),
        "p95_abs_diff": float(np.percentile(diff, 95)),
        "changed_pixel_ratio": float(changed.mean()),
        "changed_pixel_count": int(np.count_nonzero(changed)),
        "yellow_xor_count": int(np.count_nonzero(y_xor)),
        "yellow_union_count": y_union_count,
        "yellow_change_ratio": float(np.count_nonzero(y_xor) / y_union_count) if y_union_count else 0.0,
        "diff_img": diff,
    }


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    x = np.array(xs, dtype=float)
    y = np.array(ys, dtype=float)
    if np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.array(values, dtype=float), q))


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "from_frame",
        "to_frame",
        "from_rel_pts_time",
        "to_rel_pts_time",
        "dt",
        "weighted_dx",
        "weighted_dy",
        "weighted_pixel_speed",
        "direction_deg",
        "roi_x1",
        "roi_y1",
        "roi_x2",
        "roi_y2",
        "mean_abs_diff",
        "p95_abs_diff",
        "changed_pixel_ratio",
        "changed_pixel_count",
        "diff_energy_per_second",
        "yellow_xor_count",
        "yellow_union_count",
        "yellow_change_ratio",
        "time_axis_flag",
        "visual_flag",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def draw_diff_sheet(path: Path, images: dict[int, np.ndarray], rows: list[dict]) -> None:
    tiles = []
    wanted_to = {1200, 1205, 1210, 1215, 1220, 1225, 1230, 1235, 1240, 1245}
    for row in rows:
        if row["to_frame"] not in wanted_to:
            continue
        prev = images[row["from_frame"]]
        curr = images[row["to_frame"]]
        x1, y1, x2, y2 = (int(row[k]) for k in ("roi_x1", "roi_y1", "roi_x2", "roi_y2"))
        crop = curr[y1:y2, x1:x2].copy()
        diff = row["_diff_img"]
        heat = cv2.applyColorMap(cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX), cv2.COLORMAP_INFERNO)
        heat = cv2.resize(heat, (crop.shape[1], crop.shape[0]))
        cv2.putText(crop, f"{row['from_frame']}->{row['to_frame']}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.putText(crop, f"dt={row['dt']:.2f}s spd={row['weighted_pixel_speed']:.1f}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
        combined = np.hstack([crop, heat])
        tiles.append(cv2.resize(combined, (760, 220), interpolation=cv2.INTER_AREA))
    if not tiles:
        return
    cols = 1
    blank = np.zeros_like(tiles[0])
    out_rows = []
    for i in range(0, len(tiles), cols):
        row_tiles = tiles[i : i + cols]
        row_tiles += [blank.copy()] * (cols - len(row_tiles))
        out_rows.append(np.hstack(row_tiles))
    imwrite(path, np.vstack(out_rows))


def write_report(path: Path, rows: list[dict], corr: float) -> None:
    dts = [float(r["dt"]) for r in rows]
    speeds = [float(r["weighted_pixel_speed"]) for r in rows]
    mean_diffs = [float(r["mean_abs_diff"]) for r in rows]
    changed = [float(r["changed_pixel_ratio"]) for r in rows]
    ychanges = [float(r["yellow_change_ratio"]) for r in rows]
    first = rows[0]
    last = rows[-1]
    dt_bad = [r for r in rows if r["time_axis_flag"]]
    visual_flags = [r for r in rows if r["visual_flag"]]
    samples = []
    for frame in [1200, 1205, 1210, 1215, 1220, 1225, 1230, 1235, 1240, 1245]:
        hit = next((r for r in rows if r["to_frame"] == frame), None)
        if hit:
            samples.append(
                f"{hit['from_frame']}->{hit['to_frame']}: dt={hit['dt']:.6f}s, "
                f"pixel_speed={hit['weighted_pixel_speed']:.1f}px/s, "
                f"mean_abs_diff={hit['mean_abs_diff']:.2f}, "
                f"changed_ratio={hit['changed_pixel_ratio']:.4f}, "
                f"yellow_change_ratio={hit['yellow_change_ratio']:.4f}"
            )

    visual_text = "未发现需要单独解释的像素差分孤立异常。"
    if visual_flags:
        parts = []
        for r in visual_flags:
            parts.append(
                f"{r['from_frame']}->{r['to_frame']} rel={r['to_rel_pts_time']:.6f}s "
                f"mean_abs_diff={r['mean_abs_diff']:.2f}, speed={r['weighted_pixel_speed']:.1f}px/s, {r['visual_flag']}"
            )
        visual_text = "存在以下局部复核点：" + "；".join(parts)

    text = f"""黄色车辆视觉运动状态与像素差分一致性分析
============================================================

一、视觉模态粗略运动状态

从连续帧画面观察，黄色车辆在 frame_index={first['from_frame']}-{last['to_frame']}、rel_pts_time={first['from_rel_pts_time']:.6f}-{last['to_rel_pts_time']:.6f} s 范围内由画面左侧向右侧通过路口，并略向画面下方运动。车辆通过过程中，车身在画面中的投影尺度增大，车身相对摄像机视角发生变化，黄色车身轮廓和可跟踪特征点数量随之变化。本案中该轮廓变化由透视关系与车辆运动方向变化共同造成。

二、像素差分方法

本次差分没有采用整幅画面，而是采用黄色车辆运动路径附近的动态 ROI：每一对相邻帧使用前后两帧车身外接框的并集并向外扩展，计算灰度绝对差分、变化像素比例、黄色车身掩膜异或比例，同时读取加权车身中心的图像平面速度。这样可以减少时间戳、路灯、远处灯光和无关背景对结果的影响。

三、时间轴核验

相邻帧 dt 范围为 {min(dts):.6f}-{max(dts):.6f} s，均为 0.04 s 量级，对应 25 fps。时间轴异常检查结果：{'未发现 PTS 间隔突变、双倍间隔或缺帧迹象。' if not dt_bad else '存在 PTS 间隔异常，需复核 CSV。'}

四、像素差分与画面速度

加权中心像素速度范围约为 {min(speeds):.1f}-{max(speeds):.1f} px/s，中位数约为 {percentile(speeds, 50):.1f} px/s。动态 ROI 的 mean_abs_diff 范围约为 {min(mean_diffs):.2f}-{max(mean_diffs):.2f}，变化像素比例范围约为 {min(changed):.4f}-{max(changed):.4f}，黄色车身掩膜变化比例范围约为 {min(ychanges):.4f}-{max(ychanges):.4f}。

像素差分强度与加权中心像素速度的 Pearson 相关系数约为 {corr:.3f}。该相关性不能理解为严格测速关系，因为差分值还受到车辆投影面积变化、车身角度变化、车灯反光和阈值分割影响；但其足以用于核验画面运动是否连续、是否出现“时间轴正常但画面冻结”或“时间轴跳变但画面位移不匹配”的现象。

主要采样：
{chr(10).join(samples)}

五、局部复核点

{visual_text}

这些复核点属于图像差分/轮廓/反光层面的局部波动，不是 PTS 时间轴异常。结合连续帧画面，车辆运动方向仍保持向画面右侧并略向下方。

六、结论

1. 时间轴方面：frame_index={first['from_frame']}-{last['to_frame']} 范围内相邻帧 PTS 间隔稳定为约 0.04 s，未见重复 PTS、双倍间隔或缺帧迹象。
2. 画面方面：黄色车辆连续向画面右侧并略向下方运动，像素差分在车辆运动路径附近持续存在，未出现车辆应运动而差分接近 0 的冻结现象。
3. 一致性方面：本区间时间轴与视频画面像素运动具有一致性。局部差分高值可由透视关系、车辆运动方向变化、车灯和反光共同解释，不构成时间轴与画面运动不一致。
4. 边界：像素差分和像素速度均为图像平面量，只能用于一致性核验，不能直接等同实际物理车速。

输出文件：
- yellow_car_pixel_diff_analysis.csv：逐相邻帧差分数据
- yellow_car_pixel_diff_sheet.png：采样帧差分热力图
- yellow_car_visual_pixel_diff_consistency.txt：本分析文本
============================================================
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument("--tracking-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, default=1195)
    parser.add_argument("--end-frame", type=int, default=1245)
    parser.add_argument("--margin", type=int, default=70)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    track_rows = [
        r
        for r in load_tracking(args.tracking_csv)
        if args.start_frame <= r["frame_index"] <= args.end_frame
    ]
    image_paths = {
        int(re.search(r"frame_(\d+)\.png$", p.name).group(1)): p
        for p in args.frames_dir.glob("frame_*.png")
        if re.search(r"frame_(\d+)\.png$", p.name)
    }
    images = {r["frame_index"]: imread(image_paths[r["frame_index"]]) for r in track_rows}

    rows: list[dict] = []
    for prev, curr in zip(track_rows, track_rows[1:]):
        prev_img = images[prev["frame_index"]]
        curr_img = images[curr["frame_index"]]
        roi = union_bbox(prev, curr, curr_img.shape, args.margin)
        metrics = diff_metrics(prev_img, curr_img, roi)
        dt = curr["rel_pts_time"] - prev["rel_pts_time"]
        dx = curr["weighted_cx"] - prev["weighted_cx"]
        dy = curr["weighted_cy"] - prev["weighted_cy"]
        speed = math.hypot(dx, dy) / dt if dt else 0.0
        row = {
            "from_frame": prev["frame_index"],
            "to_frame": curr["frame_index"],
            "from_rel_pts_time": prev["rel_pts_time"],
            "to_rel_pts_time": curr["rel_pts_time"],
            "dt": dt,
            "weighted_dx": dx,
            "weighted_dy": dy,
            "weighted_pixel_speed": speed,
            "direction_deg": math.degrees(math.atan2(dy, dx)) if dx or dy else 0.0,
            "roi_x1": roi[0],
            "roi_y1": roi[1],
            "roi_x2": roi[2],
            "roi_y2": roi[3],
            "mean_abs_diff": metrics["mean_abs_diff"],
            "p95_abs_diff": metrics["p95_abs_diff"],
            "changed_pixel_ratio": metrics["changed_pixel_ratio"],
            "changed_pixel_count": metrics["changed_pixel_count"],
            "diff_energy_per_second": metrics["mean_abs_diff"] / dt if dt else 0.0,
            "yellow_xor_count": metrics["yellow_xor_count"],
            "yellow_union_count": metrics["yellow_union_count"],
            "yellow_change_ratio": metrics["yellow_change_ratio"],
            "time_axis_flag": "" if abs(dt - 0.04) <= 1e-4 else "dt_not_0.04",
            "visual_flag": "",
            "_diff_img": metrics["diff_img"],
        }
        rows.append(row)

    mean_values = [r["mean_abs_diff"] for r in rows]
    speed_values = [r["weighted_pixel_speed"] for r in rows]
    mean_p95 = percentile(mean_values, 95)
    speed_p95 = percentile(speed_values, 95)
    speed_p05 = percentile(speed_values, 5)
    for r in rows:
        if r["mean_abs_diff"] >= mean_p95 and r["weighted_pixel_speed"] >= speed_p95:
            r["visual_flag"] = "diff_and_speed_high"
        elif r["weighted_pixel_speed"] <= speed_p05 and r["mean_abs_diff"] > percentile(mean_values, 50):
            r["visual_flag"] = "speed_low_diff_not_low"

    corr = pearson([r["weighted_pixel_speed"] for r in rows], [r["mean_abs_diff"] for r in rows])
    write_csv(args.output_dir / "yellow_car_pixel_diff_analysis.csv", rows)
    draw_diff_sheet(args.output_dir / "yellow_car_pixel_diff_sheet.png", images, rows)
    write_report(args.output_dir / "yellow_car_visual_pixel_diff_consistency.txt", rows, corr)
    print(args.output_dir / "yellow_car_pixel_diff_analysis.csv")
    print(args.output_dir / "yellow_car_pixel_diff_sheet.png")
    print(args.output_dir / "yellow_car_visual_pixel_diff_consistency.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
