# 技术栈

采用Paddle生态与OpenCV的本地处理路线，并在每次运行中锁定实际版本、配置和模型哈希。

## 固定组件

- 使用Python组织任务、运动分析和CSV更新。
- 使用PaddleDetection的PP-YOLOE+或经本案验证的PP-Vehicle模型检测车辆。
- 使用PaddleDetection/PP-Tracking维持车辆ID；固定机位优先测试ByteTrack，遮挡或机位运动明显时再比较带外观或相机运动补偿的方案。
- 使用OpenCV Shi-Tomasi特征与金字塔LK稀疏光流测量严格相邻帧对；执行正反向误差、区域约束、方向一致性和稳健估计。
- 使用NumPy/Pandas更新分析版CSV，必要时使用Matplotlib生成异常帧标注图。

## 模型选择

- 先在真实样本上比较PP-YOLOE+不同尺寸，再锁定满足小目标召回、车辆完整度和运行时间要求的最小模型。
- 检测框混入背景或邻车时，启用PaddleDetection实例分割模型做掩膜细化；不得把模型框中心位移直接作为像素速度。

## 复现要求

保存Python、PaddlePaddle、PaddleDetection和OpenCV版本；保存模型名称、权重来源、权重SHA-256、输入尺寸、置信度阈值、跟踪配置及CPU/GPU后端。版本兼容性验证完成前不在技能正文硬编码小版本号。
