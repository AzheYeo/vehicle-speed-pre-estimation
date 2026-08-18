# 技术栈

采用Paddle生态的本地处理路线，并在每次运行中锁定实际版本、配置和模型哈希。

## 固定组件

- 使用Python组织任务、数据契约和info生成。
- 使用FFmpeg/FFprobe直接读取原视频并提取format、stream、frame、packet和无损帧图。
- 使用PaddleOCR PP-OCRv6本地识别OSD，并叠加格式约束、跨帧投票和时间状态校验。
- 使用NumPy/Pandas生成单一逐帧CSV。

## 模型选择

OSD优先比较PP-OCRv6 tiny与small；最终选择以逐帧时间准确率和跳秒边界准确率为准，不以单张图片OCR分数决定。

## 复现要求

保存Python、FFmpeg、PaddlePaddle、PaddleOCR和相关依赖版本；保存模型名称、权重来源、权重SHA-256、输入尺寸、OCR配置及CPU/GPU后端。版本兼容性验证完成前不在技能正文硬编码小版本号。
