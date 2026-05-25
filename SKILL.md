---
name: video-metadata
description: 提取视频元数据和指定时间点附近帧图片；对视频做时间轴审查；在用户标注指定车辆后，用 OpenCV 进行车身轮廓、多特征点、ROI/光流、像素差分和热力图辅助分析，计算加权像素速度并判断车辆运动状态及时间轴与画面运动是否一致。
---

# video-metadata

## 适用范围

本 skill 的工作流分为三步：

1. 时间轴审查：用 FFmpeg/FFprobe 提取视频元数据、帧表、包表、哈希和指定时间点附近帧图片，并在 TXT 中说明 GOP 结构。
2. OpenCV 画面分析：用户标注车辆后，围绕车身轮廓和车牌、车窗、车灯等明显参照物，用动态 ROI、LK 光流或多特征点跟踪计算像素位移，并用箭头/轨迹/热力图可视化。可视化图片只展示代表帧和存在问题的帧；速度矢量图必须展示同一参照点从上一帧到当前帧的箭头，并标明中心点、连续追踪点等参与判断的参照物。
3. 加权像素速度与运动状态分析：融合轮廓中心、多特征点、动态 ROI 像素差分和 PTS 时间轴，判断画面运动是否连续、车辆是近似直线还是疑似曲线/转向运动，以及时间轴与画面像素运动是否一致。

“运动状态分析”“像素差分一致性分析”“用户标注车辆后的时间轴与画面运动一致性分析”是同一件事。用户标注目标车辆后，应把视觉运动状态、车身轮廓/多特征点跟踪、动态 ROI 像素差分和 PTS 时间轴核验整合为同一套分析流程。

没有用户标注时，不要自行判断“指定车辆”。如果用户只描述“黄色车”“撞人车”“中间那辆车”，必须先导出帧图并请用户标注，或要求用户提供明确 ROI 坐标。

详细一致性分析方法见：

- `references/marked_vehicle_consistency.md`

## 总原则

- 项目文件默认使用 UTF-8。
- PowerShell 调用脚本时使用 `-LiteralPath` 或变量保存中文路径，避免多层 shell 转义损坏路径。
- 所有输出文件放在视频同级的分析目录中，不把 CSV/TXT/PNG 散落在视频根目录。
- 分析目录名称必须包含视频文件名；目录内生成的数据文件名称不再重复视频文件名。
- 用户给出的 `StartTime`、`Duration` 或“某秒附近”均按视频首帧后的相对时间理解。
- 不能把 FFprobe 的绝对 `pts_time` 直接写成视频相对时间；必须先确定 `first_pts_time`，再计算 `rel_pts_time = pts_time - first_pts_time`。
- 导出帧图时使用 `target_pts_time = first_pts_time + relative_time` 定位帧号，再按帧号导出。
- MPEG-PS 等文件可能存在容器/音频起始时间早于视频首帧的情况。提取后必须比较 `format.start_time`、音频流 `start_time`、视频流 `start_time/first_pts_time`；若差值超过 0.5 秒，TXT 说明中同时列出：
  - 视频首帧相对时间：`video_rel = pts_time - first_pts_time`
  - 播放器/容器相对时间：`container_rel = pts_time - format.start_time`
  - 偏移量：`video_start_offset = first_pts_time - format.start_time`
  后续漂移、时间轴一致性、帧图导出均统一按视频流首帧相对时间 `video_rel` 分析；容器/播放器时间只作为差异提示和换算参考，不替代视频流首帧基准。
- 像素速度和像素差分均为图像平面量，只用于一致性核验和运动状态描述，不能直接等同实际物理车速。

## 提取元数据和指定时间附近帧图

默认帧图片区间：用户只给一个时间点 `T` 时，限定为前 2 秒至后 1 秒，共 3 秒，即 `StartTime = T - 2`、`Duration = 3`。若 `T < 2`，则 `StartTime = 0`，只向后补足到最多 3 秒。

```powershell
$skillDir = "E:\000\需要发出的报告\.codex\skills\video-metadata"
$extract = Join-Path $skillDir "extract.ps1"
$video = "<视频绝对路径>"

& $extract -VideoPath $video -StartTime 47 -Duration 3
```

主要输出到 `<视频名>_video_metadata_<时间戳>\`。目录名称保留视频文件名，目录内文件使用通用短名称：

- `info_<时间戳>.txt`
- `frames_<时间戳>.csv`
- `packets_<时间戳>.csv`
- `framehash_<时间戳>.csv`
- `frames_<start>s-<end>s\frame_******.png`

提取后至少核验：

- `first_pts_time`
- `format.start_time` 与 `first_pts_time` 的差值；如不一致，说明播放器时间与视频首帧相对时间的换算关系
- 导出帧号是否按 `first_pts_time + StartTime` 换算
- 选定区间帧号是否连续
- 相邻帧 `delta_pts_time` 是否稳定
- `duration_time` 是否稳定
- GOP 划分、关键帧间隔、I/P/B 帧结构是否连续合理
- 是否存在重复 PTS、双倍间隔、缺帧或异常 packet 位置/大小

## 标注车辆后一致性分析

前置条件：用户已上传带标注图片，或提供某一帧中目标车辆 ROI 坐标 `x1,y1,x2,y2`。目标车辆必须由用户标注确定。

基础流程：

1. 换算标注 ROI 到原始视频帧坐标，并写明标注图尺寸、原始帧尺寸、换算坐标。
2. 运行基础光流跟踪脚本，生成逐帧跟踪数据和接触图。
3. 观察速度矢量复核图，确定正式结论采用的稳定帧号区间。复核图不需要展示全部帧，只展示起止帧、中间代表帧和疑似异常帧；图中应标明中心位移 `C`、连续追踪参照点 `T1...Tn`。`Tn` 必须表示同一局部参照点从上一帧到当前帧的光流位移箭头，不能表示每帧重新检测到的候选点。
4. 如果用户要求更精细、不要单点、或需要像素差分核验，读取 `references/marked_vehicle_consistency.md`，继续做车身轮廓/多特征点加权分析、动态 ROI 像素差分和轨迹/差分热力图辅助判断。
5. 最终说明必须同时回答：视觉运动状态、PTS 时间轴状态、画面像素运动状态、时间轴与画面是否一致。

基础光流脚本：

```powershell
$skillDir = "E:\000\需要发出的报告\.codex\skills\video-metadata"
$analyzer = Join-Path $skillDir "scripts\analyze_marked_vehicle.py"

python $analyzer `
  --frames-dir "<提取目录>\frames_47s-50s" `
  --frames-csv "<提取目录>\frames_<时间戳>.csv" `
  --anchor-frame <用户标注所在帧号> `
  --box "x1,y1,x2,y2" `
  --start-frame <start> `
  --end-frame <end> `
  --stable-start-frame <stable_start> `
  --stable-end-frame <stable_end> `
  --output-dir "<提取目录>\marked_vehicle_analysis" `
  --crop "x1,y1,x2,y2"
```

基础输出：

- `marked_vehicle_tracking.csv`
- `marked_vehicle_tracking_stable.csv`
- `marked_vehicle_tracking_sheet.png`：代表帧/问题帧速度矢量复核图，不是全帧列表
- `marked_vehicle_four_part_analysis.txt`

## 说明结构

正式说明建议简洁包含：

1. 分析对象和 ROI 换算。
2. 时间轴 PTS 数据：`first_pts_time`、帧号范围、`rel_pts_time` 范围、`dt` 稳定性。
3. 视觉运动状态：车辆由何处向何处运动，轮廓变化主要原因。
4. 画面像素运动：轮廓/多特征点/像素差分结果，必要时列局部复核点。
5. 一致性结论：时间轴与视频画面像素运动是否一致。
6. 边界：像素速度和像素差分不是实际物理车速。
