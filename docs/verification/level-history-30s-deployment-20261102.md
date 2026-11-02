# 液位 30 秒部署：单点队列的回滚兼容保护

2026-11-02。主机安装及入库验收已完成：本轮 `installed / healthy=true` 回执、运行与历史只读验收、完整备份内容校验和认证注册均已闭环。发布、安装和最终运行结果分开记录，详见文末及[生产只读审计](production-timing-readonly-20261102.md)。

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

主机实际源码无法通过现有只读 API 直接取得，可信历史来源为 6dd6411 安装回执和已验证发布 ZIP；安装端在正式动作前按固定三文件哈希强校验，未知漂移拒绝。本轮实际完成约 7GB 一致数据库备份，后续完整校验和认证结果见下文。停采集、等租约和预热均为实际安装流程的一部分；不把历史磁盘余量当现时事实，也不以发布包哈希冒充主机磁盘哈希。

只读入库验收已通过：运行采样约 5 秒；抽查普通点 `periodic/60` 仍约 60 秒；液位合并 `periodic/60` 和 `level-periodic/30` 后约 30 秒，额外对齐/质量/整点证据另列。`level-periodic` 补点自身通常相隔 60 秒；实现选取原始约 5 秒采样的 30 秒时间桶，不保证每条间隔精确 30.000 秒，实测一段为 25.345 秒。液位目标配对仍为 90 秒，实际配置、模型和旧历史保留。该验收为指定点和短窗口，不扩大为所有点长期均无抖动的结论。

实际 v2 独立审查：ZIP 原字节、全部 payload/控制脚本 SHA、Python 编译、PowerShell 静态语法及 8 个安装/冲突/中断/回滚场景全部通过，exit 0。业务来源仍为 f7a0196，更新器保护来源为 1ca7174；没有把两者的全部父提交同步到主机。

主代理随后最终批准发布 SHA-256=`f64398ae7aa10cf8e7ca3c2b2365f271e6dca34ab08d21a1336084f17320df3a`、32,551 字节的 v2。正式 ZIP 已原子更新到 `outputs/offline-win10-x64/extras/ProjectUpdate.zip`，原正式包完整保留为同目录 `ProjectUpdate-before-level-history-30s-20261102-211fe74718bc.zip`（原 SHA-256=`211fe74718bc90f361c41ab00b9a33ec8202184328f5ec3f25cdeb8fca4d2ec0`）。下载配置、latest 元数据、服务源码、IP/网关/防火墙均未改。

下载服务入口为 `backend/scripts/offline_transfer.py`，通过既有 `--daemon/CREATE_NO_WINDOW` 隐藏启动，实际 `10.255.2.6:8765` 已监听。无 secret 入口 `http://10.255.2.6:8765/` 自动跳转到下载页，页面标签为“下载本次项目更新包（保留主机数据库）”；页面/ZIP 均 HTTP 200，`Content-Length=32551`，从 HTTP 实际下载的 ZIP 与获批 SHA 完全相同。无 secret 发布证据在 v2 目录 `publication.json`；后续安装回执为 `outputs/offline-win10-x64/target-install-status.json` 和 `target-install-events.jsonl`。`transfer.log` 含访问能力路径，不能展开到聊天中。

## 主机安装与备份最终结果

- 本轮主机安装回执 `revision=f7a0196abde26d0adfe75ccbfa06d2a6f5303287`、`stage=installed`、`healthy=true`、`client=10.255.2.7`。主机北京时间 `12:13:53.598445`，笔记本接收时间 `12:14:32.385685`；主代理独立读取本地回执确认，未以旧 10 月 29 日回执代替。
- source payload 仅 `backend/app/telemetry/config.py`、`repository.py`、`worker.py` 三文件。更新器保护来源为 1ca7174；revision=f7a0196 不表示整个父提交链、DS 清理或研究内容已部署。
- 主机实测闭环为 **采集 5 秒、液位常规合并历史约 30 秒、普通历史 60 秒、液位目标滞后 90 秒**。两类历史周期与目标滞后分别验收，没有把 `alignment-evidence/5` 混入 30 秒周期统计。
- 数据库一致备份复制 **7078 MiB**、`1811843/1811843 pages`，耗时 **297 秒**，结构检查通过。主机报告备份目录为 `C:\Users\XMC\DshDensityControl\backups\source-update-20261102-104848-047320`。
- 主机 `12:13:52.395322` 明确回执 `Snapshot full content check passed (4667s).`，即 **完整内容校验通过，fullcheck=true**。主机 `12:13:53.567175` 随后回执完整校验结果已注册、认证恢复点受保留策略保护，故本备份为 **verified / 已认证**，不是 timeout、deferred-unverified 或预算到期后的未认证结果。
- 生产只读审计代理在笔记本 `12:17:39.722` 再核对本地状态与事件尾部，最新仍为本轮成功安装；成功回执后的短观察窗口未见后续 rollback/failed。详见[生产只读审计的部署后追加记录](production-timing-readonly-20261102.md)。

主机安装由用户本地运行，主代理与生产只读审计完成最终验收；本实现代理仅同步报告，没有再次修改代码、包、服务、主机配置或数据库，也没有在此开展研究。
