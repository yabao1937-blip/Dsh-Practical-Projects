# 手工补录新增「浮精量」指标（t/h，参与决策）· 2026-10-28

## 需求（用户 2026-10-28）

浮精量手工补录此前在页面上没有入口（只能靠决策总览浮精量卡片的旧 amountInputs 录入或
表2 停更记录兜底）。在「手工信息补录」弹窗新增**浮精量（t/h）**录入指标，并接入决策计算。

## 实现（镜像 2026-10-26 浮精灰分 0b30f95 的接入模式）

- 指标目录 `services/manual_measurements.py`：`float_clean_coal_amount`（浮精量，t/h，无
  上限、非 archive_only，参与决策）；`schemas.py` ManualMetric/ManualEntryMetric 枚举扩展。
- 数据库：`models.py` CHECK 约束扩展 + `schema_upgrade.py` 原位扩约束（KNOWN_METRIC_SETS
  登记新目录态、`include_float_amount`）+ Alembic 迁移 `0018_manual_float_amount`
  （c3a610280001，SQLite 复制表保行保索引，降级不删已有记录）。
- 决策链接线：`hourly_brief/inputs.py` 浮精量候选序 = 压滤机板数折算（保持最高优先）→
  **手工补录指标（新）** → amountInputs 旧录入 → 表2 记录，按观测/生产时间取最新；
  `decision_sources.collect_shared` 表2 记录不得掩盖更新的手工指标（镜像浮精灰分规则）。
- 更新器兼容：`apply_update.py` 回滚兼容集接受新枚举、重写时补全缺失指标（旧业务代码
  回滚后仍能读取浮精量行）、`rollback_schema_sources` 在浮精量约束存在时同样保留
  schema 兼容模块。
- 前端零改动：表单/筛选/修改弹窗均由 `/manual-measurements/metrics` 目录驱动，自动出现
  「浮精量」。

## 验证

pytest 共 337 项通过：

- 指标目录/录入（test_manual_measurements）：目录含浮精量（t/h 非归档）、POST 15 落库、
  `/latest` 点集包含新指标。
- 决策链（test_decision_sources 新增 2 项）：新录入 15 替换旧存储值 40 进入三组分总灰分
  （重介量随之为 460）；与表2 记录按时间互竞（旧手工不掩盖新表2、新手工赢表2、更新表2
  再反超）；板数折算优先级不变（既有用例）。
- 迁移与更新器：test_telemetry_migrations 头版本 c3a610280001、test_storage_migration、
  test_project_update 回滚兼容（含幂等/未知指标拒绝）全过；浮精量消费链
  （hourly_brief/coarse_amount/expert8/hourly_advice/float_output/ash_settlement）135 项
  回归通过。
- 浏览器 E2E（隔离库 Chrome）：表单选择「浮精量」录入 15 → 保存确认与记录列表显示，
  数据库核对 name/unit/value；既有修改/删除/近1个月用例不变全过。

## 未覆盖项

- 主机生产：含迁移与后端改动，走标准更新包部署；装完后现场直接在「手工信息补录」录入
  浮精量 15 即完成任务 2 的改值（板数折算运行期间以板数为准，手工值在板数停更时生效）。
