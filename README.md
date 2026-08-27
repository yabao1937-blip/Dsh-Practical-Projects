# Dsh-Practical-Projects

DeepSeek Harness (DSH) 实践项目合集：按日期归档每天完成的可运行项目。

## 项目列表

### 2026-08-26 / 27 · 重介密控系统（预测模型层）

选煤厂重介质选煤密度控制系统的「预测模型」模块：

- **XGB + LightGBM 软测量**：用实时可测工艺量推断当前无法实时测量的指标（精煤真实灰分、介质粘度、精煤水分、悬液密度、回收率），指标字典驱动、每指标独立建模
- **TCN 时序预测**：级联 + 混合架构（软测量当前估计 + 最近化验真值作静态锚点），预测「下一次化验值」（horizon = 2 小时）
- **真实数据表 schema**：SQLite 5 张表 + 指标字典，真实 PLC/化验数据就绪后可直接入库
- **交付**：训练/评估/预测 CLI + 可 import 的预测库（`dmdcs.serving.SoftSensorPredictor` / `TCNForecaster`）

详见子目录 `dense-medium-density-control-system/`（含 `docs/` 设计文档）。

> 说明：本地项目目录名为 "Dense Medium Dense Control System"（含拼写笔误），仓库内统一用正确命名 `dense-medium-density-control-system`。
