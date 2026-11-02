# 液位 30 秒部署：单点队列的回滚兼容保护

2026-11-02。用户已授权窄范围主机部署；当前完成本地实现、测试与候选准备，发布/主机安装仍由主代理在候选验收后推进。本报告不把本地通过称为主机已生效。

## 必要修正与范围

业务实现局部提交为 `f7a0196abde26d0adfe75ccbfa06d2a6f5303287`，其中三个采集业务源码保持原字节。独立审查发现，标准更新器失败后恢复旧源码时，若 active spool 留有新版 `point-history-v1` 包，旧仓库会按普通节流解释它：可能不写历史却推进最新快照并 ack，永久丢失该原始补点。该问题属于部署必要可靠性，主代理已批准最小补丁。

修正仅为 `backend/scripts/windows_offline/apply_update.py`、新增测试 `backend/tests/test_point_history_update_recovery.py` 及本报告。原有一致数据库备份、进程归属确认、自然租约等待、标准启动/健康检查和数据库不回退策略不变。没有新增跳过备份的 profile，没有调整模型、全点配置、配对滞后或采集业务实现。

## 保留与恢复协议

- 仅当候选 manifest 显式声明 `collector_spool_compatibility=point-history-v1`，采用标准 profile 且 payload 恰为已审三个 `telemetry/config.py`、`repository.py`、`worker.py` 时启用。未知标记、缺少源码或扩大的 payload 在备份前拒绝。
- 使用原 `Start.ps1` 明确指定的 `backend/data/telemetry-spool/<source_id>`，首先解析 data/telemetry-spool 根的真实存储映射。固定保留目录在该真实 source 文件夹内：`.point-history-v1-retained`。`batches/` 保留原包字节，`records/` 为逐包清单。
- 允许现有 `backend/data` 或 spool 根 junction 指向 D 盘。source_id 子目录重定向不在本次允许范围，会在备份、停服务前拒绝，不假定所有“D盘存储”都具有同样映射粒度。文件/保留目录重定向、跨卷转移和同名异内容均拒绝，不覆盖冲突。
- 旧源码恢复前必须确认本安装采集器确已停止。仅对 active `batch-*.json` 中的 `point-history-v1`：先写包含原 relative path、SHA-256、source/batch 和 `released=false` 的逐包 intent，flush/fsync 后原子发布清单，再在同一实际卷原子 rename 原包到保留区。普通格式包和已有 `.tmp` 文件不动，不输出采样正文或凭据。
- 新版三个文件全部安装且实际 SHA 与 manifest 一致后、采集器启动前，按清单恢复到 active。恢复保留 held 原件，通过同卷临时文件写入/fsync/rename 发布 active 副本，之后原子记录 `released=true`；同名同字节可幂等处理，异字节冲突立即拒绝。
- intent 已写但 move 中断时，active 原件仍在，可重试。active 已发布但 released 清单更新中断时，即使后来被新采集器提交/ack，held 原件仍在，可重放原 batch，由数据库唯一约束去重；不猜测文件失踪意味着已提交。`released=true` 且 active 已消化时不重复发布，held 仍保留作为原始证据。再次失败回滚会把尚在 active 的对应包重新标为未释放并隔离。
- 隔离、清单或字节校验失败，以及无法确认采集器停止，均硬阻止旧源码恢复/旧采集器启动，保留原始文件并明确报错；不会沿旧的“recovery_error 后仍 restart”分支启动不兼容的读取器。
- 对此窄包在长数据库备份后、停服务前再次执行源码基线校验，拒绝备份期间出现的未知源码改动。

保留区不会交给旧 `DurableSpool.pending()`，也不自动删除原始 held 证据。数据库、正常格式队列和既有原始资料不恢复为旧快照，不改生产 owner 以强占租约。

## 验证

首次协议测试：18 passed in 2.41s。随后加入 intent fsync 故障及已存在同内容目标的跨卷拒绝回归，新协议为 20 项；成熟安装器和采集器停止机制一并验证：

```powershell
# D:/dense-medium-density-control-system/backend
.venv/Scripts/python.exe -m pytest tests/test_point_history_update_recovery.py tests/test_project_update.py tests/test_stop_owned_collector.py tests/test_stop_owned_collector_handles.py -q --tb=short
```

结果：**134 passed, 1 warning in 34.25s**，exit 0。警告仍是现有 Starlette/httpx 弃用提示。测试仅临时 SQLite、模拟服务和临时目录/Windows junction，没有连接 OPC 或写生产数据库。

覆盖原始字节/清单保留、普通包和 `.tmp` 不变、重复隔离/恢复、intent fsync 或 move 中断、active 发布后状态落后（含 ack 模拟）、不同内容冲突、无清单孤件拒绝、真实临时 Windows data junction、跨卷拒绝、新源齐备后恢复、失败隔离后旧版本正常恢复、隔离/停进程确认失败阻止旧启动，以及备份期间基线漂移。业务原 68 项测试结果见 `level-history-30s-20261102.md`，本轮未改业务源码而没有重复整轮执行。

## 候选与现场验收

旧候选 `output/level-history-30s-candidate-20261102-f7a0196/` 缺少本兼容保护，已明确不得发布。新的候选、redacted manifest、源码/控制脚本哈希和保留正式下载状态不变的证据单独记录在 `output/` 的 v2 候选目录，供主代理和独立审查核验；实际 manifest 的回执地址含能力令牌，不在报告/聊天中展开。

主机实际源码无法通过现有只读 API 直接取得，可信历史来源为 6dd6411 安装回执和已验证发布 ZIP；真实安装端在正式动作前按固定三文件哈希强校验，未知漂移拒绝。标准流程仍会在主机生成约 7GB 一致数据库备份，并在停采集、等租约和预热期间短暂停顿。需现场核对安装/存储映射、备份盘当前余量，不能把历史磁盘余量当现时事实。

只读后验收：实时完整点集仍约 5 秒；普通 `periodic/60` 仍约 60 秒；液位合并 `periodic/60` 和 `level-periodic/30` 后约 30 秒，额外对齐/质量/整点证据另列。`level-periodic` 补点自身通常相隔 60 秒，不能单看这一种行断定全液位周期。液位配对仍为已核实的 90 秒，实际配置、模型和旧历史保留。

下载服务入口是 `backend/scripts/offline_transfer.py`，监听配置为 `10.255.2.6:8765`，正式 ZIP 在 `outputs/offline-win10-x64/extras/ProjectUpdate.zip`；页面源码的标签为“下载本次项目更新包（保留主机数据库）”。本轮候选阶段没有覆盖此包或改 IP/网关/防火墙；当前可达性只读核对结果交主代理协调，发布仍待候选验收。
