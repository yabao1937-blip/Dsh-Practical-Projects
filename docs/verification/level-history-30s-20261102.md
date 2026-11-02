# 精磁尾液位常规历史 30 秒入库：笔记本实现与隔离验证

日期：2026-11-02。执行：按本轮指定的 GPT 子代理实现，独立 GPT 子代理审查；最终验收由主代理完成。本报告记录已完成的代码与测试，不代表生产已切换。

## 结果与适用边界

- 已实现：同一个采集器保持原 5 秒轮询和实时全点发布，在现有全量历史周期为 60 秒时，仅给精磁尾液位补充间隔中的原始观测，形成常规液位 30 秒、其他点 60 秒的历史。没有新增采集进程。
- 本次只在笔记本编辑代码并运行模拟读取器、临时 SQLite 和临时磁盘队列。没有连接 OPC 采集、没有写生产库、没有复制 7GB 数据库、没有部署或激活模型，也没有修改原始化验和冻结训练资料。
- 本机 `backend/config/kepserver.local.json` 只读标量核对为 `poll_seconds=5`、`history_interval_seconds=5`；实际配置未改。其历史本来更细，新策略会保留现有全量 5 秒，不能称本机已切换为液位 30 秒。
- 主代理协调的生产只读核对：主机轮询为 5 秒，普通及液位 `periodic` 历史为 60 秒，观测间隔约 60.04–60.25 秒；既有 `alignment-evidence` 行的 `cadence_seconds=5`，延后回填这些对齐证据时，混合历史可能出现约 30 秒的相邻时间差。这不证明原来的常规液位周期已经是 30 秒。本次新增的是独立、按轮询当时进入队列的常规液位补点。
- 笔记本源码 `backend/app/gpt_coarse/timing.py` 的 `LEVEL_LAG_SECONDS=90`。主代理协调的主机决策存档只读实核同样为 `input_timing.level_lag_seconds=90`、`max_lookback_seconds=30`、`clock=received_at`、`selection=latest-at-or-before-target`（存档 `sampled_at=2026-11-02 09:50:05.113`，北京时间；`quality_sources` 亦为 90 秒）。用户此前提到的 60 秒已澄清为统一入库周期。本次只改液位历史存储，没有修改主机或笔记本的 90 秒配对、模型、预测流程和既有历史。

生产侧事实及对应原始读数详见[生产时序只读核对](production-timing-readonly-20261102.md)；本实现代理没有访问主机。

## 选点与配置

按实际 `PointConfig.item_id` 唯一匹配 `TIA_3_5.TIA_3_5.JMCWC/LEVEL_B`，复用现有对齐证据链使用的语义标识。展示名称、点位 key 改名均不决定选点；缺少或存在多个同语义点时，不猜测选择其他点，继续原全量历史行为。

`CollectorConfig` 新增 `level_history_interval_seconds`，默认 `30`，显式 `null` 禁用。只有它小于 `history_interval_seconds` 才补点；原全量周期为 5、15、30 秒时均保持原有更细或相同历史，不重复增加液位记录。

未来在生产 60 秒基准上使用新版采集器时，配置语义为：

```json
{
  "poll_seconds": 5,
  "history_interval_seconds": 60,
  "level_history_interval_seconds": 30
}
```

`backend/config/kepserver.example.json` 已同步这组默认值；真实点表仍需包含唯一对应的语义点，模板中的示例密度点不会被当作液位。已有安装未填新键时，新代码采用默认 30 秒；其原全量周期原样保留，因此现有 60 秒安装会在未来部署新版后启用液位补点，而现有 5/30 秒安装不会被调粗。实际安装配置、本机运行进程和主机本轮均未改动。旧历史不重写，也不补造缺失观测。

新增字段仅属于采集存储配置，不进入 `PointConfig`，没有改动 `definition_id`、模型或业务口径指纹；全量批次和实时缓存原有 `cadence_seconds` 仍使用原全量周期。租约计算也仍采用原全量周期。

## 持久化行为

1. 每次真实读取仍首先发布完整实时批次，保留约 5 秒实时更新。
2. 原全量周期、首次启动、`--once` 和质量变化保留既有全量入库规则。质量事件不推进全量周期桶；原有事件、整点证据和重启首读可能增加历史行，30/60 秒指常规周期，不是禁止保存事件证据。
3. 其余轮询仅在目标液位跨入新的 30 秒 `received_at` 时隙时，入队一个 `storage_policy=point-history-v1`、`storage_kind=level-periodic` 的单点包。全量包已经保存当前液位时，不为同一轮询再发补点包。
4. 单点包保存原 poll 的 `batch_id`、完整采样内容、原 `received_at/source_at/server_at`、质量，以及当时的点位定义。没有插值、缓存冒充观测或把原时间改为整 30 秒；漏轮询时只保存后来实际取得的观测，不填补过去时隙。
5. 仓库使用单独事务分支，只增加 `telemetry_readings`，并续心跳/租约。不更新 `point.latest`、`point.last_history_at`、`source.last_sample_at`，不调用 `remember_point/remember_density`，不触发决策写入。普通历史的二次节流时钟不受补点影响。
6. 60 秒全量包继续携带原有 `alignment_evidence/hour_boundary_evidence`，30 秒包不携带它们。同一原始 poll 已经作为液位补点保存时，后来的对齐证据通过相同 `source_id/point_key/batch_id` 引用该行；保留已有行的 `level-periodic/cadence_seconds=30`，不另外复制一行 `alignment-evidence`。对齐包自身的 60 秒契约和原始 5 秒证据元数据没有改变。

历史 API 与按时间选择液位的查询不按新 `storage_kind` 排除记录，已经验证可以读取补点。新增观测可以供之后同批样本的 60/90/120 秒比较使用，本轮没有执行比较训练。最新页面的点数、在线判断和完整全点快照不被单点包覆盖；历史消费者仍按已有口径读取真实记录。

## 失败、重启与旧队列兼容

- 只有 `spool.enqueue` 成功后才推进相应周期桶；入队失败沿用既有可见故障退出，不把未入队观测标为完成。
- SQLite BUSY/LOCKED 沿用原数据库工作线程及重试机制，不另建连接采集循环；未提交包留在队列，下次可重放。提交后 ack 失败也保留原包，数据库既有唯一约束保证同 batch 重放幂等。
- 点位更名、禁用、重新绑定后，旧补点包使用包内原始定义校验，并写回原 `TelemetryPoint` 的历史，不要求当前配置仍绑定该点，不回退或填充当前最新快照。
- 校验时保留原始点位字典计算定义身份，避免默认 `scale/offset=1/0` 经模型验证变为 `1.0/0.0` 而误判旧定义。没有修改全局身份算法或旧记录；默认值、显式整数和浮点配置均有回归覆盖。
- 旧的无 `storage_policy` 包及 `periodic-v1` 全量包仍走原路径，无数据库结构迁移。
- 重启保留原先首次完整采样入库的行为；例如 t=30 的已提交补点与重启后 t=35 的新完整观测可以同时存在。防止重复的是同一原始 batch 的回放，以及同一轮询的全量/补点双写，不会为了严格“一桶一行”丢掉重启后的新观测或破坏最新快照的原始引用。

## 验证结果

`backend/conftest.py` 在导入应用前把 `DMCS_DATABASE_URL` 指向临时库并移除远程只读配置环境入口；新增测试另外使用各用例的临时 SQLite、临时 metadata catalog、临时队列和模拟 reader。测试保留 `poll_seconds=5`，仅缩短等待，观测时间由模拟时钟每 5 秒推进。

| 检查 | 结果 |
| --- | --- |
| 新增 `tests/test_level_history_30s.py` | **23 passed in 17.54s** |
| `tests/test_collector_storage.py`、`tests/test_telemetry_minute_alignment.py`、`tests/test_minute_retention_integration.py` | **45 passed, 1 warning in 23.82s** |
| 合计（不重复计数） | **68 项通过** |
| `git diff --check` | exit 0；只有 Git 的 LF/CRLF 提示 |
| 独立审查 | 另行验证 11 个临时场景全部 exit 0；独立重跑补充后的 retention 集成测试 **1 passed, 1 warning in 4.20s**，不重复计入上述 68 项 |

警告是现有 FastAPI/Starlette 测试依赖对 `httpx` 的弃用提示，没有修改依赖。

复现命令（在 `D:/dense-medium-density-control-system/backend`，使用该目录 `.venv/Scripts/python.exe`）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_level_history_30s.py -q --tb=short
.venv/Scripts/python.exe -m pytest tests/test_collector_storage.py tests/test_telemetry_minute_alignment.py tests/test_minute_retention_integration.py -q --tb=short
```

行为覆盖包括：25 次模拟 5 秒轮询跨 0/30/60/90/120 秒；29/30 边界和漏轮询保留真实时刻；质量事件；已有 5/15/30 秒配置；关闭、缺失或歧义目标；默认及显式数值类型身份；完整最新视图、普通节流和历史 API；真实延迟选择；入队失败、数据库忙重试、提交后 ack 失败重启；旧点位定义和旧全量队列；60 秒证据复用；原决策引用与冻结小时数据；31 天冷归档后以原 ID/时间/数值/种类/周期读回补点。保留策略未改。

## 交付文件与未执行项

- 采集配置：`backend/app/telemetry/config.py`、`backend/config/kepserver.example.json`。
- 采集与持久化：`backend/app/telemetry/worker.py`、`backend/app/telemetry/repository.py`。
- 新增与调整测试：`backend/tests/test_level_history_30s.py`、`backend/tests/test_telemetry_minute_alignment.py`、`backend/tests/test_minute_retention_integration.py`。
- 本报告：`docs/verification/level-history-30s-20261102.md`。

代码和本地验证完成；上述八个实现、测试与验收文件纳入本次独立本地提交。用户随后已授权部署，目前进入窄范围候选包准备，尚未发布或在主机安装；本报告不把代码完成称为生产已生效。已有用户文件、配置备份、输出目录和其他研究文档全部保留。
