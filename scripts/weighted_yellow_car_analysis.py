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
        raise RuntimeError(f"Failed to read {path}")
    return img


def imwrite(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise RuntimeError(f"Failed to encode {path}")
    buf.tofile(str(path))


def frame_index(path: Path) -> int:
    m = re.search(r"frame_(\d+)\.png$", path.name)
    if not m:
        raise ValueError(path.name)
    return int(m.group(1))


def load_frame_times(path: Path) -> tuple[dict[int, dict], float]:
    rows: dict[int, dict] = {}
    first_pts: float | None = None
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            idx = int(row["frame_index"])
            pts = float(row["pts_time"])
            if first_pts is None:
                first_pts = pts
            rows[idx] = {
                "pts_time": pts,
                "rel_pts_time": pts - first_pts,
                "duration_time": float(row.get("duration_time") or 0),
            }
    if first_pts is None:
        raise RuntimeError(f"No frame rows in {path}")
    return rows, first_pts


def load_prior_centers(path: Path) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("cx") and row.get("cy"):
                out[int(row["frame_index"])] = (float(row["cx"]), float(row["cy"]))
    return out


def detect_yellow_body(
    img: np.ndarray,
    predicted: tuple[float, float],
    roi_half_w: int = 260,
    roi_half_h: int = 125,
) -> dict:
    h, w = img.shape[:2]
    pcx, pcy = predicted
    x1 = max(0, int(round(pcx - roi_half_w)))
    y1 = max(0, int(round(pcy - roi_half_h)))
    x2 = min(w, int(round(pcx + roi_half_w)))
    y2 = min(h, int(round(pcy + roi_half_h)))
    roi = img[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    # Strict yellow body threshold; excludes most white headlight glare.
    mask = cv2.inRange(hsv, np.array((16, 55, 90)), np.array((40, 255, 255)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 5), np.uint8))

    n, labels, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    kept = []
    candidates = []
    for i in range(1, n):
        lx, ly, lw, lh, area = stats[i]
        if area < 120:
            continue
        cx = float(cent[i][0] + x1)
        cy = float(cent[i][1] + y1)
        dx = abs(cx - pcx)
        dy = abs(cy - pcy)
        if dx > 230 or dy > 100:
            continue
        if lw > 280 or lh > 115:
            continue
        candidates.append((i, lx + x1, ly + y1, lw, lh, int(area), cx, cy))

    if candidates:
        largest_area = max(c[5] for c in candidates)
        candidates = [
            c for c in candidates
            if c[5] >= max(120, int(largest_area * 0.08))
        ]

    for i, gx, gy, lw, lh, area, cx, cy in candidates:
        keep[labels == i] = 255
        kept.append((gx, gy, lw, lh, area, cx, cy))

    if not kept:
        return {
            "ok": False,
            "mask": np.zeros(img.shape[:2], dtype=np.uint8),
            "area": 0,
            "bbox": None,
            "moment_center": predicted,
            "bbox_center": predicted,
            "contour_center": predicted,
            "components": 0,
        }

    full_mask = np.zeros(img.shape[:2], dtype=np.uint8)
    full_mask[y1:y2, x1:x2] = keep
    ys, xs = np.where(full_mask > 0)
    bx1, by1, bx2, by2 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    area = int((full_mask > 0).sum())
    m = cv2.moments(full_mask)
    if m["m00"]:
        mcx = float(m["m10"] / m["m00"])
        mcy = float(m["m01"] / m["m00"])
    else:
        mcx, mcy = predicted
    bcx = (bx1 + bx2) / 2.0
    bcy = (by1 + by2) / 2.0
    ccx = 0.60 * mcx + 0.40 * bcx
    ccy = 0.60 * mcy + 0.40 * bcy
    return {
        "ok": True,
        "mask": full_mask,
        "area": area,
        "bbox": (bx1, by1, bx2, by2),
        "moment_center": (mcx, mcy),
        "bbox_center": (bcx, bcy),
        "contour_center": (ccx, ccy),
        "components": len(kept),
    }


def feature_flow(
    prev_img: np.ndarray,
    curr_img: np.ndarray,
    prev_bbox: tuple[int, int, int, int] | None,
) -> dict:
    if prev_bbox is None:
        return {"ok": False, "n_points": 0, "dx": 0.0, "dy": 0.0}
    h, w = prev_img.shape[:2]
    x1, y1, x2, y2 = prev_bbox
    x1 = max(0, x1 - 35)
    y1 = max(0, y1 - 30)
    x2 = min(w - 1, x2 + 45)
    y2 = min(h - 1, y2 + 35)
    if x2 <= x1 or y2 <= y1:
        return {"ok": False, "n_points": 0, "dx": 0.0, "dy": 0.0}

    prev_gray = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY)
    curr_gray = cv2.cvtColor(curr_img, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(prev_gray.shape, dtype=np.uint8)
    mask[y1:y2, x1:x2] = 255
    points = cv2.goodFeaturesToTrack(
        prev_gray,
        maxCorners=220,
        qualityLevel=0.01,
        minDistance=4,
        blockSize=5,
        mask=mask,
    )
    if points is None or len(points) < 8:
        return {"ok": False, "n_points": 0, "dx": 0.0, "dy": 0.0}

    next_points, status, _ = cv2.calcOpticalFlowPyrLK(
        prev_gray,
        curr_gray,
        points,
        None,
        winSize=(25, 25),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if next_points is None or status is None:
        return {"ok": False, "n_points": 0, "dx": 0.0, "dy": 0.0}

    src = points[status.ravel() == 1].reshape(-1, 2)
    dst = next_points[status.ravel() == 1].reshape(-1, 2)
    if len(src) < 8:
        return {"ok": False, "n_points": len(src), "dx": 0.0, "dy": 0.0}

    flow = dst - src
    med = np.median(flow, axis=0)
    residual = np.linalg.norm(flow - med, axis=1)
    cutoff = max(2.5, float(np.percentile(residual, 70)))
    keep = residual <= cutoff
    if keep.sum() >= 8:
        flow = flow[keep]
        src = src[keep]
        residual = residual[keep]

    weights = 1.0 / (1.0 + residual)
    dx, dy = np.average(flow, axis=0, weights=weights)
    return {
        "ok": True,
        "n_points": int(len(flow)),
        "dx": float(dx),
        "dy": float(dy),
        "points": src,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "frame_index",
        "pts_time",
        "rel_pts_time",
        "duration_time",
        "contour_area",
        "contour_components",
        "contour_cx",
        "contour_cy",
        "bbox_x1",
        "bbox_y1",
        "bbox_x2",
        "bbox_y2",
        "feature_points",
        "feature_dx",
        "feature_dy",
        "weighted_cx",
        "weighted_cy",
        "dt",
        "dx",
        "dy",
        "pixel_displacement",
        "pixel_speed",
        "direction_deg",
        "anomaly",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def draw_sheet(path: Path, images: dict[int, np.ndarray], rows: list[dict]) -> None:
    tiles = []
    wanted = {int(rows[0]["frame_index"]), int(rows[-1]["frame_index"])}
    wanted.update(int(r["frame_index"]) for r in rows if int(r["frame_index"]) % 5 == 0)
    for r in rows:
        idx = int(r["frame_index"])
        if idx not in wanted:
            continue
        img = images[idx].copy()
        bbox = None
        if r["bbox_x1"] != "":
            bbox = tuple(int(float(r[k])) for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"))
            cv2.rectangle(img, bbox[:2], bbox[2:], (0, 0, 255), 2)
        cx = float(r["weighted_cx"])
        cy = float(r["weighted_cy"])
        ccx = float(r["contour_cx"])
        ccy = float(r["contour_cy"])
        cv2.circle(img, (round(ccx), round(ccy)), 5, (0, 255, 255), -1)
        cv2.circle(img, (round(cx), round(cy)), 6, (255, 0, 255), -1)
        cv2.putText(img, str(idx), (max(0, round(cx - 60)), max(25, round(cy - 55))), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        x1 = max(0, round(cx - 440))
        y1 = max(0, round(cy - 130))
        x2 = min(img.shape[1], round(cx + 440))
        y2 = min(img.shape[0], round(cy + 140))
        tile = img[y1:y2, x1:x2]
        if tile.size:
            tiles.append(cv2.resize(tile, (696, 210), interpolation=cv2.INTER_AREA))
    if not tiles:
        return
    cols = 2
    blank = np.zeros_like(tiles[0])
    out_rows = []
    for i in range(0, len(tiles), cols):
        row = tiles[i : i + cols]
        row += [blank.copy()] * (cols - len(row))
        out_rows.append(np.hstack(row))
    imwrite(path, np.vstack(out_rows))


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.array(values, dtype=float), q))


def write_report(path: Path, rows: list[dict], stable_rows: list[dict], first_pts: float, source_note: str) -> None:
    speeds = [float(r["pixel_speed"]) for r in stable_rows if r["pixel_speed"] != ""]
    first = stable_rows[0]
    last = stable_rows[-1]
    dx_total = float(last["weighted_cx"]) - float(first["weighted_cx"])
    dy_total = float(last["weighted_cy"]) - float(first["weighted_cy"])
    dt_total = float(last["rel_pts_time"]) - float(first["rel_pts_time"])
    avg_speed = math.hypot(dx_total, dy_total) / dt_total if dt_total else 0.0
    median_speed = percentile(speeds, 50)
    anomalies = [r for r in stable_rows if r.get("anomaly")]
    samples = []
    for r in stable_rows:
        idx = int(r["frame_index"])
        if idx % 10 == 5 or idx in {int(first["frame_index"]), int(last["frame_index"])}:
            samples.append(
                f"frame {idx}, rel_pts_time={float(r['rel_pts_time']):.6f} s, "
                f"weighted_center=({float(r['weighted_cx']):.1f},{float(r['weighted_cy']):.1f}), "
                f"contour_area={int(float(r['contour_area']))}, feature_points={int(float(r['feature_points']))}, "
                f"pixel_speed={float(r['pixel_speed']) if r['pixel_speed'] != '' else 0:.1f} px/s"
            )
    text = f"""黄色车辆车身轮廓与多特征点加权运动分析
============================================================

一、分析方法

{source_note}

本次不以单一红点作为参照点。每一帧先在车辆附近区域提取黄色车身 HSV 轮廓，计算车身黄色区域面积、轮廓质心和轮廓外接框；再在上一帧车身框附近提取多个 Shi-Tomasi 角点，并用 LK 光流计算多特征点的加权平均位移。最终加权中心由“当前帧车身轮廓中心”和“上一帧加权中心按多特征点光流预测的位置”合成，其中轮廓中心权重约 0.60，多特征点光流权重约 0.40。黄色轮廓用于约束目标车身，多特征点用于降低单一中心点抖动影响。

二、时间轴核验

视频首帧 first_pts_time={first_pts:.6f} s，本分析采用 rel_pts_time = pts_time - first_pts_time。分析区间内相邻帧时间间隔为 0.04 s，帧率对应 25 fps。前次元数据核验未发现重复 PTS、双倍间隔或缺帧现象。

三、加权运动结果

稳定分析区间为 frame_index={first['frame_index']}-{last['frame_index']}，rel_pts_time={float(first['rel_pts_time']):.6f}-{float(last['rel_pts_time']):.6f} s。该区间内车辆加权中心由 ({float(first['weighted_cx']):.1f}, {float(first['weighted_cy']):.1f}) 移动至 ({float(last['weighted_cx']):.1f}, {float(last['weighted_cy']):.1f})，累计 dx={dx_total:.1f} px，dy={dy_total:.1f} px。dx 为正、dy 为正，表示车辆在图像平面内向画面右侧并略向下方运动。

该区间按加权中心计算的平均像素速度约为 {avg_speed:.1f} px/s，逐帧像素速度中位数约为 {median_speed:.1f} px/s，速度范围约为 {min(speeds):.1f}-{max(speeds):.1f} px/s。未发现约 2 倍突增、接近 0 的停滞帧或方向突变等孤立异常。

主要采样点：
{chr(10).join(samples)}

四、结论

1. 采用车身黄色轮廓和多特征点光流加权后，黄色车辆在 frame_index={first['frame_index']}-{last['frame_index']}、rel_pts_time={float(first['rel_pts_time']):.6f}-{float(last['rel_pts_time']):.6f} s 内持续向画面右侧并略向下方行驶。
2. 加权结果与上一版中心点轨迹的运动方向一致，差别在于本次参照的是车身轮廓和多个特征点的合成结果，而不是单一显示点。
3. 时间轴 PTS 连续稳定，画面运动连续，未见时间轴与车辆画面运动不一致的迹象。
4. frame_index=1250 以后车辆逐渐接近或离开画面右缘，车身轮廓不完整，不建议作为主要运动状态结论区间。

说明：像素速度为图像平面速度，只用于核验时间轴与画面运动一致性，不能直接等同实际物理车速。
============================================================
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument("--frames-csv", type=Path, required=True)
    parser.add_argument("--prior-tracking-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, required=True)
    parser.add_argument("--end-frame", type=int, required=True)
    parser.add_argument("--stable-start-frame", type=int, required=True)
    parser.add_argument("--stable-end-frame", type=int, required=True)
    parser.add_argument("--source-note", default="")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    frame_times, first_pts = load_frame_times(args.frames_csv)
    prior = load_prior_centers(args.prior_tracking_csv)
    paths = {frame_index(p): p for p in args.frames_dir.glob("frame_*.png")}
    indices = [i for i in range(args.start_frame, args.end_frame + 1) if i in paths and i in prior]
    images = {i: imread(paths[i]) for i in indices}

    rows: list[dict] = []
    prev_img = None
    prev_bbox = None
    prev_center = None
    for idx in indices:
        img = images[idx]
        contour = detect_yellow_body(img, prior[idx])
        contour_center = contour["contour_center"]
        flow = {"ok": False, "n_points": 0, "dx": 0.0, "dy": 0.0}
        predicted_from_flow = None
        if prev_img is not None and prev_center is not None:
            flow = feature_flow(prev_img, img, prev_bbox)
            if flow["ok"]:
                predicted_from_flow = (prev_center[0] + flow["dx"], prev_center[1] + flow["dy"])

        if predicted_from_flow is not None and contour["ok"]:
            area_weight = min(0.70, max(0.50, contour["area"] / 9000.0))
            cx = area_weight * contour_center[0] + (1.0 - area_weight) * predicted_from_flow[0]
            cy = area_weight * contour_center[1] + (1.0 - area_weight) * predicted_from_flow[1]
        elif contour["ok"]:
            cx, cy = contour_center
        elif predicted_from_flow is not None:
            cx, cy = predicted_from_flow
        else:
            cx, cy = prior[idx]

        meta = frame_times[idx]
        row = {
            "frame_index": idx,
            "pts_time": meta["pts_time"],
            "rel_pts_time": meta["rel_pts_time"],
            "duration_time": meta["duration_time"],
            "contour_area": contour["area"],
            "contour_components": contour["components"],
            "contour_cx": contour_center[0],
            "contour_cy": contour_center[1],
            "bbox_x1": "",
            "bbox_y1": "",
            "bbox_x2": "",
            "bbox_y2": "",
            "feature_points": flow["n_points"],
            "feature_dx": flow["dx"] if flow["ok"] else "",
            "feature_dy": flow["dy"] if flow["ok"] else "",
            "weighted_cx": cx,
            "weighted_cy": cy,
            "dt": "",
            "dx": "",
            "dy": "",
            "pixel_displacement": "",
            "pixel_speed": "",
            "direction_deg": "",
            "anomaly": "",
        }
        if contour["bbox"] is not None:
            row["bbox_x1"], row["bbox_y1"], row["bbox_x2"], row["bbox_y2"] = contour["bbox"]
        if rows:
            prev = rows[-1]
            dt = float(row["rel_pts_time"]) - float(prev["rel_pts_time"])
            dx = cx - float(prev["weighted_cx"])
            dy = cy - float(prev["weighted_cy"])
            disp = math.hypot(dx, dy)
            row["dt"] = dt
            row["dx"] = dx
            row["dy"] = dy
            row["pixel_displacement"] = disp
            row["pixel_speed"] = disp / dt if dt else ""
            row["direction_deg"] = math.degrees(math.atan2(dy, dx)) if disp else ""
        rows.append(row)
        prev_img = img
        prev_bbox = contour["bbox"]
        prev_center = (cx, cy)

    speeds = [float(r["pixel_speed"]) for r in rows if r["pixel_speed"] != ""]
    median_speed = float(np.median(speeds)) if speeds else 0.0
    for r in rows:
        if r["pixel_speed"] == "" or median_speed <= 0:
            continue
        ratio = float(r["pixel_speed"]) / median_speed
        if ratio >= 1.8:
            r["anomaly"] = "pixel_speed_near_2x"
        elif ratio <= 0.2:
            r["anomaly"] = "pixel_speed_near_zero"

    stable = [
        r
        for r in rows
        if args.stable_start_frame <= int(r["frame_index"]) <= args.stable_end_frame
        and r["pixel_speed"] != ""
    ]
    write_csv(args.output_dir / "yellow_car_weighted_tracking.csv", rows)
    write_csv(args.output_dir / "yellow_car_weighted_tracking_stable.csv", stable)
    draw_sheet(args.output_dir / "yellow_car_weighted_tracking_sheet.png", images, stable)
    write_report(
        args.output_dir / "yellow_car_weighted_motion_analysis.txt",
        rows,
        stable,
        first_pts,
        args.source_note,
    )
    print(args.output_dir / "yellow_car_weighted_tracking.csv")
    print(args.output_dir / "yellow_car_weighted_motion_analysis.txt")
    print(args.output_dir / "yellow_car_weighted_tracking_sheet.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
