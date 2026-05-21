---
name: video-metadata
description: 提取视频元数据和指定时间点附近帧图片；在用户标注指定车辆后，分析时间轴 PTS 与画面像素运动是否一致，并生成四部分说明。
---

# video-metadata

## 适用范围

本 skill 只做两类工作：

1. 提取视频元数据，以及用户指定时间点附近的帧图片。
2. 用户在图片中标出指定车辆后，对该车辆做时间轴与画面运动一致性分析，并生成说明。

不要在用户未标注目标车辆时自行判断“指定车辆”。如果用户只描述“黄色车”“撞人车”“中间那辆车”，必须先导出帧图并请用户在图片中标出目标车辆，或请用户提供明确 ROI 坐标。

## 总原则

- 项目文件默认使用 UTF-8。
- PowerShell 调用脚本时使用 `-LiteralPath` 或变量保存中文路径，避免多层 shell 转义损坏路径。
- 所有输出文件放在视频同级的分析目录中，不把 CSV/TXT/PNG 散落在视频根目录。
- 不能把 FFprobe 的绝对 `pts_time` 直接写成视频相对时间；必须先确定首帧 `first_pts_time`，再计算 `rel_pts_time = pts_time - first_pts_time`。
- 像素速度是图像平面速度，只用于核验时间轴与画面运动一致性、辅助描述车辆运动状态；不得直接等同为实际物理车速。
- 判断画面异常时，优先看“时间轴是否正常递增”和“像素速度是否同步连续”。若时间轴正常但画面像素速度出现孤立异常，应提示人工复核，而不是直接下结论。

## 功能一：提取元数据和指定时间点附近帧图

### 默认时间窗口

用户只给一个时间点时，默认取前 2 秒至后 2 秒，共 4 秒。

示例：

- 用户说“1分14秒附近”：按 `StartTime=72`、`Duration=4` 处理。
- 用户说“74秒附近”：按 `StartTime=72`、`Duration=4` 处理。

### 运行命令

```powershell
$skillDir = "E:\000\需要发出的报告\.codex\skills\video-metadata"
$extract = Join-Path $skillDir "extract.ps1"
$video = "<视频绝对路径>"

# 元数据、帧表、包表、帧哈希，并导出指定时间段帧图片
& $extract -VideoPath $video -StartTime 72 -Duration 4

# 指定输出目录
& $extract -VideoPath $video -StartTime 72 -Duration 4 -OutputDir "<输出目录>"
```

### 主要输出

`extract.ps1` 输出到 `<视频名>_video_metadata_<时间戳>\`：

- `<视频名>_info_<时间戳>.txt`
- `<视频名>_frames_<时间戳>.csv`
- `<视频名>_packets_<时间戳>.csv`
- `<视频名>_framehash_<时间戳>.csv`
- `frames_<start>s-<end>s\frame_******.png`

### 提取后必须核验

提取完成后至少核验：

- 首帧 `first_pts_time`。
- 选定区间帧号是否连续。
- 相邻帧 `delta_pts_time` 是否稳定。
- `duration_time` 是否稳定。
- 是否存在重复 `pts_time`、双倍间隔、缺帧或异常 packet 位置/大小。

## 功能二：用户标注车辆后的时间轴与画面运动一致性分析

### 前置条件

必须满足以下任一条件，才能做正式“指定车辆”分析：

- 用户上传带标注的图片，并明确红框/箭头/圈选的是目标车辆。
- 用户提供目标车辆在某一帧中的 ROI 坐标：`x1,y1,x2,y2`。

如果没有用户标注，只能输出“需要用户标注车辆后才能分析”的说明。

### 分析对象和坐标

- 目标车辆由用户标注确定。
- ROI 坐标使用原始视频帧坐标，原点为左上角。
- `x` 向右为正，`y` 向下为正。
- `dy < 0` 表示目标在画面中向上移动。

如果用户上传的标注图不是原始 1920x1080 尺寸，应按图片显示尺寸与原始帧尺寸比例换算 ROI。换算结果必须在说明中写明，例如：

```text
用户标注图尺寸为 1155x641，原始视频帧为 1920x1080；
红框换算至原始画面约为 x1=780,y1=325,x2=940,y2=420。
```

### 运行命令

使用通用脚本：

```powershell
$skillDir = "E:\000\需要发出的报告\.codex\skills\video-metadata"
$analyzer = Join-Path $skillDir "scripts\analyze_marked_vehicle.py"

python $analyzer `
  --frames-dir "<提取目录>\frames_72s-76s" `
  --frames-csv "<提取目录>\<视频名>_frames_<时间戳>.csv" `
  --anchor-frame 1899 `
  --box "780,325,940,420" `
  --start-frame 1840 `
  --end-frame 1899 `
  --stable-start-frame 1860 `
  --stable-end-frame 1899 `
  --output-dir "<提取目录>\marked_vehicle_analysis" `
  --crop "250,260,1120,470"
```

参数说明：

- `--anchor-frame`：用户标注 ROI 所在帧号。
- `--box`：用户标注车辆在原始帧中的坐标 `x1,y1,x2,y2`。
- `--start-frame` / `--end-frame`：尝试跟踪和分析的帧范围。
- `--stable-start-frame` / `--stable-end-frame`：正式写入结论的可靠跟踪范围。若前后有遮挡、目标与其它车辆混杂或跟踪漂移，应缩小稳定范围。
- `--crop`：输出接触图的裁剪区域，便于人工核验。

### 输出文件

`analyze_marked_vehicle.py` 输出：

- `marked_vehicle_tracking.csv`：完整尝试跟踪结果。
- `marked_vehicle_tracking_stable.csv`：正式结论采用的稳定区间数据。
- `marked_vehicle_tracking_sheet.png`：带跟踪点和框的接触图。
- `marked_vehicle_four_part_analysis.txt`：四部分分析说明。

## 像素速度核验规则

对相邻帧或相邻核验点计算：

```text
dt = rel_pts_time(i+1) - rel_pts_time(i)
dx = cx(i+1) - cx(i)
dy = cy(i+1) - cy(i)
pixel_displacement = sqrt(dx^2 + dy^2)
pixel_speed = pixel_displacement / dt
direction_deg = atan2(dy, dx)
```

重点检查：

- 时间轴 `delta_pts_time` 正常，但 `pixel_speed` 约为前后稳定值的 2 倍：疑似跳帧、ROI 跟踪错误、遮挡或画面异常，必须人工复核。
- 时间轴 `delta_pts_time` 正常，但 `pixel_speed` 接近 0，且目标应持续运动：疑似重复帧、冻结帧、ROI 跟踪错误或目标被遮挡，必须人工复核。
- 时间轴 `delta_pts_time` 约为正常值 2 倍，且 `pixel_displacement` 也约为前后正常值 2 倍：优先判断为时间间隔变化与画面位移匹配，不应直接认定画面异常。
- 方向角突然大幅变化：结合画面判断是转弯、变道、遮挡、ROI 错跟还是画面异常。

异常判断必须给出：

- 异常帧号。
- 对应 `rel_pts_time`。
- `delta_pts_time`。
- `pixel_speed`。
- 与前后稳定值或中位数的比值。
- 是否需要人工逐帧复核。

## 行驶状态描述规则

根据用户标注车辆的像素速度和方向变化描述状态：

- 像素速度在一段时间内波动较小：图像平面内接近匀速行驶。
- 像素速度连续增大：图像平面内加速行驶。
- 像素速度连续减小：图像平面内减速行驶。
- 方向角连续变化且画面轨迹弯曲：转弯、变向或变道行驶。
- 车辆中心点位移连续但受透视影响明显时，只描述“图像平面像素速度变化”，不要推断实际物理速度。

每个状态必须写明：

- 帧号范围。
- `rel_pts_time` 范围。
- 像素速度变化范围。
- 运动方向。

## 四部分说明格式

正式说明必须包含四部分：

### 第一部分：分析时间轴 PTS 数据

必须写明：

- 视频文件和提取范围。
- `first_pts_time`。
- `rel_pts_time` 计算方式。
- 分析帧号范围和对应相对时间范围。
- 帧号是否连续。
- `delta_pts_time`、`duration_time` 是否稳定。
- 是否存在 PTS 跳变、重复时间戳、双倍间隔或缺帧迹象。

### 第二部分：视频画面指定车辆的速度矢量分析

必须写明：

- 目标车辆由用户标注确定，不能自行判断。
- 标注 ROI 坐标。
- 跟踪方法和稳定跟踪范围。
- 采样帧或逐帧的 `dx`、`dy`、`pixel_speed`、方向角。
- 是否存在约 2 倍、接近 0 或方向突变的像素速度异常。

### 第三部分：结合时间轴和视频画面，分析一致性

必须写明：

- 时间轴是否线性递增。
- 画面像素运动是否连续。
- 时间轴正常时，像素速度是否同步连续。
- 若出现异常，说明是疑似跳帧、重复帧、冻结帧、ROI 跟踪异常、遮挡还是需要人工复核。

### 第四部分：结论

至少必须包含两项：

1. 时间轴与视频画面运动是否一致。
2. 指定车辆的运动状态及对应时间。

示例：

```text
1. 时间轴与视频画面一致性：在 frame_index=1860-1899、rel_pts_time=74.40-75.96 s 范围内，PTS 时间轴连续稳定，用户标注车辆画面运动连续，未发现像素速度约 2 倍、接近 0 或方向突变的异常帧；该区间时间轴与视频画面具有一致性。
2. 指定车辆运动状态：用户标注车辆在 rel_pts_time=74.40-75.96 s 内持续向画面右侧并略向上行驶。
3. 行驶状态对应时间：该车在 rel_pts_time=74.40-75.20 s 内像素速度由约 266.85 px/s 缓慢降至约 213.88 px/s，表现为轻微减速；在 rel_pts_time=75.20-75.96 s 内像素速度由约 213.88 px/s 继续降至约 143.07 px/s，表现为较明显减速。
```

## 不足和边界

- 像素速度不是实际车速。
- 车辆遮挡、反光、阴影、目标出入画面、与其它车辆重叠、ROI 漂移都会影响像素速度。
- 对自动跟踪不稳定的区间，不得强行写入正式结论；应要求用户补充该时段标注图，或人工逐帧建立车身框后再分析。
- 若自动脚本输出的接触图显示跟踪点离开目标车辆，必须缩小稳定分析区间或重新标注，不得继续引用该区间像素速度结论。

