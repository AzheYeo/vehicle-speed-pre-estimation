# vehicle-speed-pre-estimation

Codex skill for video metadata extraction, timeline review, and vehicle pixel-motion consistency analysis.

## 功能

1. 提取视频元数据、帧表、包表、帧哈希和指定时间点附近帧图片。
2. 检查 PTS 时间轴、GOP/I/P/B 帧结构、相邻帧间隔、重复 PTS、双倍间隔、缺帧和异常 packet。
3. 用户标注车辆后，用 OpenCV 做车身轮廓、多特征点、LK 光流、动态 ROI 像素差分和热力图辅助分析。
4. 融合轮廓中心、多特征点、ROI 像素差分和 PTS 时间轴，计算加权像素速度并判断车辆运动状态。
5. 输出时间轴与视频画面像素运动是否一致的结论；像素速度只用于一致性核验，不等同实际物理车速。

## 主要文件

- `SKILL.md`：skill 使用说明、分析边界和输出要求。
- `extract.ps1`：提取视频元数据、帧表、包表、帧哈希和帧图片。
- `references/marked_vehicle_consistency.md`：标注车辆后一致性分析方法。
- `scripts/analyze_marked_vehicle.py`：基于用户标注 ROI 做基础光流跟踪和四部分说明。
- `scripts/analyze_frame_interval.py`：选定帧区间辅助分析脚本。
- `scripts/parse_ps_pes.py`：原始 PS/PES/PTS 复核脚本。
- `scripts/weighted_yellow_car_analysis.py`：车身轮廓、多特征点和加权像素速度分析示例脚本。
- `scripts/yellow_car_pixel_diff_analysis.py`：动态 ROI 像素差分、热力图和局部异常复核脚本。

## 提取视频数据

```powershell
$skillDir = "<本仓库路径>"
$extract = Join-Path $skillDir "extract.ps1"
& $extract -VideoPath "<视频绝对路径>" -StartTime 47 -Duration 3
```

主要输出到 `<视频名>_video_metadata_<时间戳>\`：

- `info_<时间戳>.txt`
- `frames_<时间戳>.csv`
- `packets_<时间戳>.csv`
- `framehash_<时间戳>.csv`
- `frames_<start>s-<end>s\frame_******.png`

## 标注车辆后分析

```powershell
$analyzer = Join-Path "<本仓库路径>" "scripts\analyze_marked_vehicle.py"
python $analyzer `
  --frames-dir "<提取目录>\frames_47s-50s" `
  --frames-csv "<提取目录>\frames_<时间戳>.csv" `
  --anchor-frame "<用户标注所在帧号>" `
  --box "x1,y1,x2,y2" `
  --start-frame "<start>" `
  --end-frame "<end>" `
  --stable-start-frame "<stable_start>" `
  --stable-end-frame "<stable_end>" `
  --output-dir "<提取目录>\marked_vehicle_analysis" `
  --crop "x1,y1,x2,y2"
```

基础输出：

- `marked_vehicle_tracking.csv`
- `marked_vehicle_tracking_stable.csv`
- `marked_vehicle_tracking_sheet.png`
- `marked_vehicle_four_part_analysis.txt`

## 结论边界

- 用户没有标注目标车辆时，不自行判断“指定车辆”。
- 用户给出的时间点按视频首帧后的相对时间理解。
- MPEG-PS 等文件要区分播放器/容器相对时间和视频流首帧相对时间。
- 像素速度、像素差分和热力图只用于判断时间轴与画面运动一致性，不直接换算实际车速。

详细规则见 `SKILL.md` 和 `references/marked_vehicle_consistency.md`。
