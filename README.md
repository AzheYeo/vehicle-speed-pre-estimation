# Vehicle Video Analysis

一个面向车辆视频前置审查的纯 Skills Plugin，分离直接提取的数据与后续运动连续性推断。

## 包含的 Skills

- `video-frame-extraction`：按播放器时间区间导出帧图片、文件与视频流摘要，以及单一逐帧CSV。
- `video-motion-continuity`：读取第一阶段产物，对指定区间执行严格相邻帧运动连续性复核，并生成独立的运动分析CSV。

两个 Skill 均不计算实际物理车速，也不凭单项指标认定法律意义上的删帧或具体处理软件。

## 插件结构

插件入口为 `.codex-plugin/plugin.json`，Skills 位于 `skills/`。每个 Skill 独立保存其说明、UI元数据和按需加载的参考文件。

## 开发验证

修改后应分别校验两个 Skill，再校验插件 manifest。代表性触发请求与边界用例保存在 `evals/`。
