#!/usr/bin/env python3
"""
Parse MPEG-PS/PES-like surveillance exports directly and aggregate video PES
fragments into access units by PTS.

This script is intended for cases where ffprobe can decode images only after
demuxing or reports a guessed/synthetic time base. It does not replace ffprobe
for standard files; it adds a raw-file timing cross-check.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


PS_STREAM_IDS_WITH_LENGTH = set(range(0xBC, 0xF0))
PS_STREAM_IDS_WITH_LENGTH.update({0xFD})
PS_START_IDS = set(range(0xB9, 0xF0))
PS_START_IDS.update({0xFD})


def decode_pts(buf: bytes) -> int | None:
    if len(buf) < 5:
        return None
    return (
        (((buf[0] >> 1) & 0x07) << 30)
        | (buf[1] << 22)
        | (((buf[2] >> 1) & 0x7F) << 15)
        | (buf[3] << 7)
        | ((buf[4] >> 1) & 0x7F)
    )


def decode_scr_mpeg2(buf: bytes) -> tuple[int | None, int | None]:
    if len(buf) < 10 or (buf[0] & 0xC0) != 0x40:
        return None, None
    base = (
        (((buf[0] & 0x38) >> 3) << 30)
        | ((buf[0] & 0x03) << 28)
        | (buf[1] << 20)
        | (((buf[2] & 0xF8) >> 3) << 15)
        | ((buf[2] & 0x03) << 13)
        | (buf[3] << 5)
        | ((buf[4] & 0xF8) >> 3)
    )
    ext = ((buf[4] & 0x03) << 7) | ((buf[5] & 0xFE) >> 1)
    return base, ext


def find_start(data: bytes, pos: int) -> int:
    return data.find(b"\x00\x00\x01", pos)


def find_next_ps_start(data: bytes, pos: int) -> int:
    i = pos
    while True:
        i = find_start(data, i)
        if i < 0 or i + 4 > len(data):
            return -1
        if data[i + 3] in PS_START_IDS:
            return i
        i += 4


def parse_int(value: str) -> int:
    if value.lower().startswith("0x"):
        return int(value, 16)
    return int(value)


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_raw_file(path: Path, video_stream_id: int) -> tuple[list[dict], list[dict], Counter, Counter]:
    data = path.read_bytes()
    n = len(data)
    pos = 0
    pack_index = -1
    pack_rows: list[dict] = []
    video_rows: list[dict] = []
    stream_counts: Counter = Counter()
    unknown_starts: Counter = Counter()
    video_pes_index = 0

    while True:
        i = find_start(data, pos)
        if i < 0 or i + 4 > n:
            break

        sid = data[i + 3]

        if sid == 0xBA:
            scr_base, scr_ext = decode_scr_mpeg2(data[i + 4 : i + 14])
            stuffing = (data[i + 13] & 0x07) if i + 14 <= n and scr_base is not None else 0
            header_size = 14 + stuffing if scr_base is not None else 12
            pack_index += 1
            pack_rows.append(
                {
                    "pack_index": pack_index,
                    "file_offset": i,
                    "scr_base_90k": "" if scr_base is None else scr_base,
                    "scr_ext_27m": "" if scr_ext is None else scr_ext,
                    "scr_seconds": "" if scr_base is None else (scr_base * 300 + scr_ext) / 27000000,
                    "header_size": header_size,
                }
            )
            pos = i + max(header_size, 4)
            continue

        if sid == 0xB9:
            pos = i + 4
            continue

        if sid not in PS_STREAM_IDS_WITH_LENGTH:
            unknown_starts[sid] += 1
            pos = i + 4
            continue

        if i + 6 > n:
            break

        packet_len = (data[i + 4] << 8) | data[i + 5]
        if packet_len:
            end = i + 6 + packet_len
        else:
            end = find_next_ps_start(data, i + 6)
            if end < 0:
                end = n
        if end <= i + 6 or end > n:
            end = n

        stream_counts[sid] += 1

        if sid == video_stream_id:
            payload_start = i + 6
            flags1 = flags2 = header_len = ""
            pts = dts = None

            if i + 9 <= n and (data[i + 6] & 0xC0) == 0x80:
                flags1 = data[i + 6]
                flags2 = data[i + 7]
                header_len = data[i + 8]
                payload_start = i + 9 + header_len
                pts_dts_flags = (flags2 >> 6) & 0x03
                if pts_dts_flags in (2, 3) and i + 14 <= n:
                    pts = decode_pts(data[i + 9 : i + 14])
                if pts_dts_flags == 3 and i + 19 <= n:
                    dts = decode_pts(data[i + 14 : i + 19])

            if payload_start > end:
                payload_start = end

            video_rows.append(
                {
                    "video_pes_index": video_pes_index,
                    "stream_id_hex": f"0x{sid:02X}",
                    "file_offset": i,
                    "pack_index": pack_index,
                    "packet_length": packet_len,
                    "payload_start": payload_start,
                    "payload_size": max(0, end - payload_start),
                    "flags1_hex": "" if flags1 == "" else f"0x{flags1:02X}",
                    "flags2_hex": "" if flags2 == "" else f"0x{flags2:02X}",
                    "header_data_length": header_len,
                    "pts_90k": "" if pts is None else pts,
                    "pts_seconds_abs": "" if pts is None else pts / 90000,
                    "dts_90k": "" if dts is None else dts,
                    "dts_seconds_abs": "" if dts is None else dts / 90000,
                }
            )
            video_pes_index += 1

        pos = end

    return video_rows, pack_rows, stream_counts, unknown_starts


def aggregate_access_units(video_rows: list[dict]) -> list[dict]:
    access_units: list[dict] = []
    current: dict | None = None

    for row in video_rows:
        payload_size = int(row["payload_size"])
        pts = row["pts_90k"]

        if pts != "":
            if current is not None:
                access_units.append(current)
            current = {
                "au_index": len(access_units),
                "first_video_pes_index": int(row["video_pes_index"]),
                "first_file_offset": int(row["file_offset"]),
                "pack_index": int(row["pack_index"]),
                "pts_90k": int(pts),
                "pts_abs_sec": float(row["pts_seconds_abs"]),
                "fragment_count": 1,
                "total_payload_size": payload_size,
                "max_fragment_payload": payload_size,
            }
        elif current is not None:
            current["fragment_count"] += 1
            current["total_payload_size"] += payload_size
            current["max_fragment_payload"] = max(current["max_fragment_payload"], payload_size)

    if current is not None:
        access_units.append(current)

    if not access_units:
        return access_units

    first_pts = access_units[0]["pts_90k"]
    prev = None
    for row in access_units:
        row["pts_rel_sec"] = (row["pts_90k"] - first_pts) / 90000
        if prev is None:
            row["delta_pts_90k"] = ""
            row["delta_pts_sec"] = ""
        else:
            delta = row["pts_90k"] - prev["pts_90k"]
            row["delta_pts_90k"] = delta
            row["delta_pts_sec"] = delta / 90000
        prev = row

    deltas = [row["delta_pts_90k"] for row in access_units if row["delta_pts_90k"] != ""]
    nominal_delta = Counter(deltas).most_common(1)[0][0] if deltas else None

    for row in access_units:
        delta = row["delta_pts_90k"]
        if delta == "" or nominal_delta is None:
            row["delta_class"] = ""
        elif delta < nominal_delta * 1.5:
            row["delta_class"] = "short"
        elif delta < nominal_delta * 2.5:
            row["delta_class"] = "double"
        else:
            row["delta_class"] = "long"
        row["is_local_payload_peak"] = ""

    for idx in range(1, len(access_units) - 1):
        access_units[idx]["is_local_payload_peak"] = (
            1
            if access_units[idx]["total_payload_size"] > access_units[idx - 1]["total_payload_size"]
            and access_units[idx]["total_payload_size"] > access_units[idx + 1]["total_payload_size"]
            else 0
        )

    return access_units


def build_second_bins(access_units: list[dict]) -> list[dict]:
    bins: dict[int, dict] = {}
    for row in access_units:
        second = int(row["pts_rel_sec"])
        bucket = bins.setdefault(
            second,
            {
                "second_index": second,
                "frames": 0,
                "short": 0,
                "double": 0,
                "long": 0,
                "payload_peaks": 0,
                "first_au_index": row["au_index"],
                "last_au_index": row["au_index"],
            },
        )
        bucket["frames"] += 1
        bucket["last_au_index"] = row["au_index"]
        if row["delta_class"] in ("short", "double", "long"):
            bucket[row["delta_class"]] += 1
        if row["is_local_payload_peak"] == 1:
            bucket["payload_peaks"] += 1
    return [bins[k] for k in sorted(bins)]


def write_summary(
    path: Path,
    source: Path,
    video_rows: list[dict],
    pack_rows: list[dict],
    access_units: list[dict],
    second_bins: list[dict],
    stream_counts: Counter,
    unknown_starts: Counter,
    outputs: dict[str, Path],
) -> None:
    deltas = [row["delta_pts_90k"] for row in access_units if row.get("delta_pts_90k") != ""]
    delta_counts = Counter(deltas)
    class_counts = Counter(row.get("delta_class", "") for row in access_units)

    lines: list[str] = []
    lines.append(f"source={source}")
    lines.append(f"source_size={source.stat().st_size}")
    for key, value in outputs.items():
        lines.append(f"{key}={value}")
    lines.append(f"pack_count={len(pack_rows)}")
    lines.append(f"video_pes_count={len(video_rows)}")
    lines.append(f"access_unit_count={len(access_units)}")

    if access_units:
        span = (access_units[-1]["pts_90k"] - access_units[0]["pts_90k"]) / 90000
        fps = (len(access_units) - 1) / span if span > 0 else 0
        lines.append(f"pts_span_sec={span:.6f}")
        lines.append(f"average_fps_by_pts={fps:.6f}")

    if delta_counts:
        nominal = delta_counts.most_common(1)[0][0]
        lines.append("delta_pts_distribution_top=" + ", ".join(f"{k}:{v}" for k, v in delta_counts.most_common(12)))
        lines.append(f"nominal_delta_90k={nominal}")
        lines.append(f"nominal_delta_sec={nominal / 90000:.9f}")
        lines.append(f"nominal_fps={90000 / nominal:.6f}")

    lines.append("delta_class_counts=" + ", ".join(f"{k or 'blank'}:{v}" for k, v in class_counts.items()))
    lines.append("stream_counts=" + ", ".join(f"0x{k:02X}:{v}" for k, v in sorted(stream_counts.items())))
    if unknown_starts:
        lines.append("unknown_start_codes=" + ", ".join(f"0x{k:02X}:{v}" for k, v in unknown_starts.most_common(20)))

    if second_bins:
        lines.append(
            "first_10_second_bins="
            + "; ".join(
                "sec={second_index},frames={frames},double={double},long={long},peaks={payload_peaks},au={first_au_index}-{last_au_index}".format(
                    **row
                )
                for row in second_bins[:10]
            )
        )

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse raw MPEG-PS/PES timing data from a surveillance export.")
    parser.add_argument("video", type=Path, help="Input video/export file path.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to a timestamped directory beside the input file.",
    )
    parser.add_argument("--video-stream-id", default="0xE0", help="Video PES stream id. Default: 0xE0.")
    args = parser.parse_args()

    source = args.video.resolve()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir.resolve() if args.output_dir else source.parent / f"{source.stem}_raw_pts_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)
    video_stream_id = parse_int(args.video_stream_id)

    stem = source.stem
    video_pes_csv = output_dir / f"{stem}_original_video_pes_{ts}.csv"
    pack_scr_csv = output_dir / f"{stem}_original_pack_scr_{ts}.csv"
    access_unit_csv = output_dir / f"{stem}_original_access_units_{ts}.csv"
    second_bins_csv = output_dir / f"{stem}_original_second_bins_{ts}.csv"
    summary_txt = output_dir / f"{stem}_original_parse_summary_{ts}.txt"

    video_rows, pack_rows, stream_counts, unknown_starts = parse_raw_file(source, video_stream_id)
    access_units = aggregate_access_units(video_rows)
    second_bins = build_second_bins(access_units)

    write_csv(
        video_pes_csv,
        video_rows,
        [
            "video_pes_index",
            "stream_id_hex",
            "file_offset",
            "pack_index",
            "packet_length",
            "payload_start",
            "payload_size",
            "flags1_hex",
            "flags2_hex",
            "header_data_length",
            "pts_90k",
            "pts_seconds_abs",
            "dts_90k",
            "dts_seconds_abs",
        ],
    )
    write_csv(
        pack_scr_csv,
        pack_rows,
        ["pack_index", "file_offset", "scr_base_90k", "scr_ext_27m", "scr_seconds", "header_size"],
    )
    write_csv(
        access_unit_csv,
        access_units,
        [
            "au_index",
            "first_video_pes_index",
            "first_file_offset",
            "pack_index",
            "pts_90k",
            "pts_abs_sec",
            "pts_rel_sec",
            "delta_pts_90k",
            "delta_pts_sec",
            "delta_class",
            "fragment_count",
            "total_payload_size",
            "max_fragment_payload",
            "is_local_payload_peak",
        ],
    )
    write_csv(
        second_bins_csv,
        second_bins,
        [
            "second_index",
            "frames",
            "short",
            "double",
            "long",
            "payload_peaks",
            "first_au_index",
            "last_au_index",
        ],
    )

    outputs = {
        "video_pes_csv": video_pes_csv,
        "pack_scr_csv": pack_scr_csv,
        "access_unit_csv": access_unit_csv,
        "second_bins_csv": second_bins_csv,
        "summary_txt": summary_txt,
    }
    write_summary(summary_txt, source, video_rows, pack_rows, access_units, second_bins, stream_counts, unknown_starts, outputs)

    print(f"video_pes_csv={video_pes_csv}")
    print(f"pack_scr_csv={pack_scr_csv}")
    print(f"access_unit_csv={access_unit_csv}")
    print(f"second_bins_csv={second_bins_csv}")
    print(f"summary_txt={summary_txt}")
    if access_units:
        span = (access_units[-1]["pts_90k"] - access_units[0]["pts_90k"]) / 90000
        fps = (len(access_units) - 1) / span if span > 0 else 0
        print(f"access_unit_count={len(access_units)}")
        print(f"pts_span_sec={span:.6f}")
        print(f"average_fps_by_pts={fps:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
