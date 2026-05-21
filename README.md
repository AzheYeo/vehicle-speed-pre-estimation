# vehicle-speed-pre-estimation

Codex skill for video metadata extraction and vehicle pixel-motion consistency analysis.

## 功能

1. 提取视频元数据、帧表、包表、帧哈希，以及用户指定时间点附近的帧图片。
2. 用户标注指定车辆后，分析 PTS 时间轴与画面像素运动是否一致，并生成四部分说明。

## 主要文件

- `SKILL.md`：skill 使用说明和分析规则。
- `extract.ps1`：提取视频元数据和帧图片。
- `scripts/analyze_marked_vehicle.py`：根据用户标注车辆做时间轴与画面运动一致性分析。
- `scripts/analyze_frame_interval.py`：选定帧区间辅助分析脚本。
- `scripts/parse_ps_pes.py`：原始 PS/PES/PTS 复核脚本。

## 基本用法

```powershell
$skillDir = "<本仓库路径>"
$extract = Join-Path $skillDir "extract.ps1"
& $extract -VideoPath "<视频绝对路径>" -StartTime 72 -Duration 4
```

```powershell
$analyzer = Join-Path "<本仓库路径>" "scripts\analyze_marked_vehicle.py"
python $analyzer `
  --frames-dir "<提取目录>\frames_72s-76s" `
  --frames-csv "<提取目录>\<视频名>_frames_<时间戳>.csv" `
  --anchor-frame 1899 `
  --box "780,325,940,420" `
  --start-frame 1840 `
  --end-frame 1899 `
  --stable-start-frame 1860 `
  --stable-end-frame 1899 `
  --output-dir "<提取目录>\marked_vehicle_analysis"
```

详细规则见 `SKILL.md`。
