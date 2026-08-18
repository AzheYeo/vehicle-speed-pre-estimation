# 数据契约

不同实现可以省略不适用字段，但不得改变字段语义。原始值、格式化值和质量值并存；不得以平滑值覆盖原始测量。

## 帧图片与逐帧映射

逐帧清单至少保存：

```text
图片帧序号
PTS时间_s
OSD时间
OSD秒内帧序号
图片文件
```

- `图片帧序号`：以原视频实际可解码首帧为1，按全片解码显示顺序得到的整数。截取局部区间时保留全片序号，不得从1重新编号；不得用声明帧率和PTS公式推算替代实际解码排序。
- `图片文件`：统一为`frame_n.png`。令`width = len(str(max(本次全部图片帧序号)))`，文件名使用`frame_{图片帧序号:0{width}d}.png`。不得固定宽度，也不得重排原帧号。例如帧序号集合为`{7, 42, 105}`时，命名为`frame_007.png`、`frame_042.png`、`frame_105.png`。
- 默认不输出“导出序号”或“源帧序号”。若用户明确要求额外编号，必须注明定义，且不得改变`图片帧序号`。
- `OSD秒内帧序号`与`图片帧序号`语义不同，不得混用。前者不得参与帧图命名。
- CSV行、帧图文件名、OSD秒统计和证据图标签必须使用同一`图片帧序号`。

## 逐车辆帧对记录

每一行只对应一个目标的一个严格相邻帧对：

```text
from_frame
to_frame
from_rel_pts_time
to_rel_pts_time
delta_pts_time
osd_time
osd_second
osd_second_status
target_id
segment_id
method
evaluation_status
dx_px
dy_px
displacement_px
projected_displacement_px
local_expected_displacement_px
motion_ratio
valid_point_count
forward_backward_error_px
direction_consistency
box_x
box_y
box_w
box_h
candidate_type
notes
```

`displacement_px`专指 `sqrt(dx²+dy²)`；沿主运动方向的量必须写入 `projected_displacement_px`，二者不得混用。

`evaluation_status`至少允许：`valid`、`candidate`、`low_quality`、`not_evaluable`。

## 重叠测量

重叠帧对为每辆车分别保留一行，并增加：

```text
overlap_group
overlap_target_count
```

不得把不同车辆的 `dx_px`、`dy_px` 或原始像素位移相加或平均。

## 唯一帧汇总

需要一行一个到达帧时，至少输出：

```text
帧序号
OSD时间
OSD秒完整性
成品OSD秒间帧间隔数
倍率推定原始帧间隔数
dx_px
dy_px
代表车辆倍率
运动不连续共识倍率
倍率来源
推定原始帧间隔增量
采用车辆
重叠车辆数
重叠车辆倍率范围
valid_point_count
forward_backward_error_px
from_frame
to_frame
```

- `代表车辆倍率`必须能由同一行代表车辆的投影位移和局部基线相除得到。
- `运动不连续共识倍率`：单车时等于代表车辆倍率；重叠时使用各车倍率的稳健共识（默认中位数）。
- `倍率来源`至少取 `single_target` 或 `overlap_consensus`；重叠时同时保存各车独立记录、倍率范围和共识算法。
- `帧序号`默认指 `to_frame`；`OSD时间`默认记录该到达帧画面显示的OSD。改变归属 convention 时必须在配置和报告中声明。
- `dx_px/dy_px`：来自质量最优的代表车辆，必须在“采用车辆”中标明，不代表跨车共识。
- `推定原始帧间隔增量`：按本案配置对共识倍率分级得到。
- `成品OSD秒间帧间隔数`：相邻OSD跳秒锚点间的成品帧间隔数。
- `倍率推定原始帧间隔数`：同一完整OSD秒内各唯一帧对推定增量之和。若需要兼容用户字段名“OSD秒间帧数”，必须在表头或元数据中明确标注“倍率推定”，不得与成品间隔数混用。

## OSD秒统计

```text
osd_second
first_frame
last_frame
encoded_frame_intervals
candidate_count
inferred_original_intervals
boundary
```

完整OSD秒可以因帧相位产生24/26等摆动；模型判断同时检查多秒平均值、候选次数和候选节律，不强求每秒合计完全相同。

## 人工复核精简表

为便于筛选和逐帧肉眼复核，另行输出：

```text
帧序号
PTS时间_s
OSD时间
OSD秒间帧数
dx_px
dy_px
运动不连续共识倍率
异常标记
```

精简表口径：

- `OSD秒间帧数` 是倍率推定的秒内运行累计值，不是行号，也不是重复填充的秒级总数。
- 每行先按共识倍率得到 `inferred_step`，再计算 `OSD秒间帧数 = 当前显示OSD秒内截至本行的 Σ inferred_step`；显示OSD秒变化时重新从0累计。
- `异常标记` 在共识倍率达到本案候选阈值且质量合格时写入“异常”，否则留空。
- `dx_px`、`dy_px` 仍来自质量最优的代表车辆；精简表不表达跨车原始位移共识。
- 精简表只服务人工复核，不得删除或替代完整汇总表、逐车辆帧对记录和重叠独立记录。

## 来源信息

结构化产物应记录：输入文件SHA-256、工具及版本、命令/参数、ROI/锚点及其来源、目标是否由用户指定、阈值配置、生成时间和证据相对路径。候选必须能追溯到具体帧对和图片。

候选表使用共识倍率时，还必须输出代表车辆倍率、共识倍率、倍率来源、重叠车辆数、倍率范围及独立记录位置。另行输出 `not_evaluable` 帧对清单；即使数量为0，也要提供空表或明确的结构化计数。
