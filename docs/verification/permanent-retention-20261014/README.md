# 数据永久保留验证 · 2026-10-14

- 用户要求取消数据库定期清理，改为永久存储。
- 取消原始采集历史的 7 天删除策略、每分钟清理调度；旧 `retention_days` 自动归一为 `0`，旧 `prune` 入口不执行删除。
- 取消整库恢复备份“仅保留最近 20 份”的删除逻辑。
- 保留逐次决策的无损压缩归档及自动解压查询；采集缓存只在成功入库后确认删除。显式人工删除、恢复备份的功能未改动。
- 本机 `backend/config/kepserver.local.json` 已改为 `retention_days: 0`（本地配置不入 Git）。Web 和独立采集进程均已重启；采集启动日志确认 `history_retention=permanent`。
- 测试：`test_telemetry.py`、`test_telemetry_retained.py`、`test_decision_references.py`、`test_hourly_brief.py`、`test_backup.py`、`test_telemetry_control.py`、`test_training.py` 共 107 项通过，均使用隔离数据库。覆盖 400 天前原始样本、坏质量样本、旧配置兼容、归档后读回、25 份恢复快照及 DS 原版对拍。
- 现场：新采集器运行超过 3 分钟后，42 个批次均完整保存 30 点；HTTP 健康检查 200、30/30 点可用。
- 与重启前一致备份逐行比较，707,380 条既有采集记录无删除或修改；24,685 条逐次决策、40 条小时统计、7 条人工补录和现有保存模型/启用版本均保留。249 条超过 7 天且未被决策引用的原始记录仍存在。
- 备份路径与完整证据见 `before.json`、`verification.json`。DS 对拍基线 SHA-256 未变。

此修改只防止今后自动清理，不自动恢复旧规则曾删除的历史。历史归档/验收文档中的旧策略是当时记录，现行策略见运行维护文档。
