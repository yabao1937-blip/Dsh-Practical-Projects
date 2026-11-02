# 主机采集、入库与液位滞后只读核查（2026-11-02）

检查目标：`http://10.255.2.7:8000/`。首次检查窗口为笔记本北京时间 2026-11-02 09:46:23—09:53:38，09:57:29 补核 `checked_at` 语义和 HTTP `Date`。主机时间字段下文统一换算为 UTC+08:00，原始接口使用 UTC。主机报告时钟和笔记本存在约 38—39 秒差，见下文，不能直接跨机相减判断采集是否停顿。

## 结论

- 主机运行缓存明确发布 `poll_seconds=5.0`、`cadence_seconds=60.0`；前者是采集周期，后者是普通历史入库周期。
- 普通点位最近 6 条历史均为 `storage_kind=periodic`、`cadence_seconds=60.0`，接收时间相邻间隔 60.044—60.250 秒。
- 精磁尾液位普通周期记录也为 60 秒；另有 `alignment-evidence` 对齐证据记录。它们混合排列时会出现约 30 秒的相邻间隔，不能据此称普通历史已经改为 30 秒。
- 主机已保存决策的 `input_timing` 和液位 `alignment` 明确记录 `level_lag_seconds=90`、`max_lookback_seconds=30`。入库 60 秒与液位目标滞后 90 秒是两项独立设置。
- 实核的一条决策，目标为预测时刻前 90 秒，实际引用的接收时间为预测前 91.221 秒；原始源时间为预测前 101.479 秒。后两个数是实际观测时间差，不是配置滞后。
- 检查结束时主机采集 `connected`、`error_code=null`；最近 6 次液位采集实际间隔 4.937—5.047 秒。按主机时钟，最新采样年龄 0.703 秒。

## 轻量接口与响应耗时

| 检查 | HTTP / 查询结果 | 耗时 | 关键证据 |
|---|---|---:|---|
| `GET /api/v1/health/live` | 200 | 69 ms | `status=ok`、`version=0.3.0`、`uptime_seconds=343061.5` |
| `GET /api/v1/telemetry/latest`（首次） | 200 | 47 ms | `kepserver-main=connected`、无错误、28 个点 |
| `GET /api/v1/readonly-db/live-cache`（首次认证读取） | 200 | 125 ms | `poll_seconds=5.0`、`cadence_seconds=60.0`、`retry_seconds=0` |
| `GET /api/v1/telemetry/latest`（识别点位） | 200 | 62 ms | JMCWC/LEVEL_B 与 403/MD_SET 均为 `good` |
| 普通点历史，近 8 分钟、`LIMIT 6` | 6 条 | 63 ms | 均为 `periodic / 60.0` |
| 液位历史，限定起始时间、`LIMIT 6` | 6 条 | 109 ms | 普通周期与对齐证据并存 |
| 最新已有决策，限定起始时间、`LIMIT 1` | 1 条 | 78 ms | 明确保存 90 秒液位滞后策略 |
| 同一决策按主键读取（提取引用） | 1 条 | 63 ms | `$reading=4980496` |
| 引用的采样按主键读取 | 1 条 | 31 ms | 对齐目标、真实接收时间与源时间闭环 |
| `GET /api/v1/readonly-db/live-cache`（结束核查） | 200 | 140 ms | 采集正常、实际 5 秒周期、时钟差估算 |
| `GET /api/v1/health/live`（补核 HTTP `Date`） | 200 | 66 ms | 独立于液位缓存的主机响应时间，同样比笔记本早约 39 秒 |

另有一次同一决策主键读取用于定位 `overlay.alignment`，47 ms。未遇到繁忙、认证失败或超时，没有连续重试。

## 实际历史记录

来源为 `kepserver-main`。以下均为主机数据库中的接收时间，不是笔记本采集的替代数据。

### 普通点：403/MD_SET

点位 `TIA_3_2.TIA_3_2.403/MD_SET`，key=`tag_67b6a6a0092ad596317f`。

| 记录 ID | 主机北京时间 `received_at` | 类型 | 声明周期 |
|---:|---|---|---:|
| 4980413 | 09:47:03.755 | periodic | 60 秒 |
| 4980384 | 09:46:03.700 | periodic | 60 秒 |
| 4980355 | 09:45:03.581 | periodic | 60 秒 |
| 4980326 | 09:44:03.537 | periodic | 60 秒 |
| 4980297 | 09:43:03.287 | periodic | 60 秒 |
| 4980268 | 09:42:03.162 | periodic | 60 秒 |

按表中倒序排列，相邻时间差为 `60.055, 60.119, 60.044, 60.250, 60.125` 秒。

### 精磁尾液位：JMCWC/LEVEL_B

点位 `TIA_3_5.TIA_3_5.JMCWC/LEVEL_B`，key=`tag_543c440c9a90e0e2daae`。

| 记录 ID | 主机北京时间 `received_at` | 类型 | `cadence_seconds` |
|---:|---|---|---:|
| 4980447 | 09:48:03.870 | periodic | 60 |
| 4980418 | 09:47:03.755 | periodic | 60 |
| 4980438 | 09:46:33.747 | alignment-evidence | 5 |
| 4980389 | 09:46:03.700 | periodic | 60 |
| 4980409 | 09:45:33.685 | alignment-evidence | 5 |
| 4980360 | 09:45:03.581 | periodic | 60 |

只比较 `periodic` 记录，相邻时间差为 `60.115, 60.055, 60.119` 秒。全部类型混合比较，则为 `60.115, 30.008, 30.047, 30.015, 30.104` 秒。`alignment-evidence` 的 `cadence_seconds=5` 标记其采样来源周期，不能解释为该点全部历史每 5 秒入库。

## 主机液位滞后与实际观测时间

只读查询主机已保存的 `decision_cycle_samples`，未调用可能计算当前或小时决策的接口。

- 决策主键 `slot=358716841`，`sampled_at=2026-11-02T01:50:05.113+00:00`。
- `input_timing.version=gpt-level-lag-v1`。
- `level_lag_seconds=90`、`max_lookback_seconds=30`、`clock=received_at`。
- `selection=latest-at-or-before-target`、`training_rows=already-aligned`。
- `quality_sources` 同时保存该液位 `lag_seconds=90`。
- `cells.level.overlay.alignment` 和 `inputs.level.overlay.alignment` 均保存下列目标与偏差；液位状态 `good`。

| 时间字段 | 主机北京时间 | 相对预测时刻 |
|---|---|---:|
| `prediction_at` | 09:50:05.113 | 0 |
| `target_at` | 09:48:35.113 | 提前 90.000 秒 |
| 引用记录 4980496 的 `received_at` | 09:48:33.892 | 提前 91.221 秒 |
| 同一记录的 `source_at` / `server_at` | 09:48:23.634 | 提前 101.479 秒 |

该引用记录为精磁尾液位，`storage_kind=alignment-evidence`、`cadence_seconds=5`、`quality=good`。目标与接收时间之差按序列化时间戳计算为 1.221 秒；快照保存的更高精度 `offset_seconds=1.221729`。源时间到接收时间另有 10.258 秒差，本次未追查其形成原因。

因此，这条决策的配置滞后是 90 秒；91.221 秒为实际接收年龄，101.479 秒为实际源时间年龄。不能把观测年龄误报成修改后的滞后参数。

## 采集实际周期和两机时钟差

`checked_at` 不是运行缓存文件的上次写入时间。已核对本地该端点实现：`backend/app/remote_readonly/server.py:405—409` 的 GET 处理函数在每次请求时调用 `transport_view(current.path)`；`backend/app/remote_readonly/live_cache.py:14` 在读取运行缓存前执行 `checked_at=iso_time(utc_now())`；`backend/app/telemetry/domain.py:11—12` 的 `utc_now()` 为 `datetime.now(timezone.utc)`。缓存内的 `published_at` 是另一个字段。此处核对的是本地接口实现语义，未读取部署主机源码，因此另用主机实际 HTTP 响应作交叉验证。

结束核查时，笔记本北京时间 `09:53:38.699` 收到主机 `checked_at=09:53:00.173` 的运行缓存，往返 140 ms。以客户端请求开始/结束中点估算，该次主机报告时刻比笔记本慢约 **38.453 秒**。

09:57:29 仅补发一次轻量 `GET /api/v1/health/live`：返回 200、66 ms、`server=uvicorn`、`status=ok`、`uptime_seconds=343726.9`。笔记本 UTC 请求开始/结束为 `2026-11-02T01:57:28.979Z` / `2026-11-02T01:57:29.045Z`，主机 HTTP `Date` 为 `Mon, 02 Nov 2026 01:56:50 GMT`，比客户端中点早 **39.012 秒**；未出现 `Age` 或 `Via` 头。HTTP `Date` 为整秒，不能拿这个差值替代毫秒精度校时，但其方向和约 38—39 秒量级与前次 `checked_at` 估算一致。接口生成语义和独立健康响应的时间共同支持主机报告时钟与笔记本有差，不能将前述时间差直接归因为液位缓存延迟。

未校准外部标准时钟，不能据此判定哪台机器正确；也未修改任何时钟。

主机 `published_at=09:52:59.470`，与主机 `checked_at` 相差 0.703 秒。最新 6 批液位接收时间为：

`09:52:34.455, 09:52:39.455, 09:52:44.470, 09:52:49.517, 09:52:54.533, 09:52:59.470`。

相邻间隔为 `5.000, 5.015, 5.047, 5.016, 4.937` 秒。初次读取时相对笔记本约 38—40 秒的时间差，不能用来推断采集停滞；主机内部时间轴与最新批次支持采集正常。

## 查询边界与未核实项

历史小查询使用既有 `ix_telemetry_point_time` 索引，分别限定两个点、近几分钟和 `LIMIT 6`：

```sql
SELECT id, received_at, sample
FROM telemetry_readings INDEXED BY ix_telemetry_point_time
WHERE source_id = ? AND point_key = ? AND received_at >= ?
ORDER BY received_at DESC LIMIT 6;
```

普通点起始时间为 `2026-11-02T01:39:53.901+00:00`；液位起始时间为 `2026-11-02T01:40:00.000+00:00`。决策查询限定 `sampled_at >= '2026-11-02T01:35:00.000+00:00'` 并 `LIMIT 1`，随后均按已取得的主键读取。

认证使用既有 `remote_mode.settings()` / `remote_sqlite.RemoteConnection`，内部加载已存在凭据。凭据未打印、未写入报告、未放入命令行，也未发往外网。每次网络请求超时为 3 秒（最初公共 HTTP 请求为 3.5 秒），请求顺序执行；只读桥接事务全部释放。

本次未直接读取主机采集配置 JSON 或主机程序源码；采集和入库的运行值来自主机自身发布元数据及真实历史记录，液位策略来自主机真实决策快照。未对全部 28 个点逐点审计，未评估长期周期抖动；液位实际偏差只实核这一条已保存决策，不代表每条决策都具有相同偏差。源时间与接收时间差的成因、两机时钟差的成因未核实。

没有部署、重启、修改主机配置、改生产参数、改采集/入库周期、触发小时重算或写入生产业务数据；本地仅新增本报告。

## 本轮液位 30 秒发布后的运行与最终安装验收

本节为随后用户授权主机部署后的追加记录，不替换上文 09:46—09:57 的部署前基线。部署执行由主代理协调；本只读检查代理没有部署、停止或重启服务，没有校时或修改主机配置。

**最终状态：本轮安装已完成，`installed / healthy=true`；备份完整内容校验 `fullcheck=true`（4667 秒）且认证注册成功（verified）。** 笔记本 `12:17:39.722` 再次核对本地状态与事件尾部，最新同 revision 事件仍为成功安装，成功回执之后没有新事件，未见随后 rollback / failed 回执。此前主机运行实测已证明采集 5 秒、液位合并周期约 30 秒、抽查普通点 60 秒与液位目标滞后 90 秒；本轮没有重复生产查询。

主代理提供的本轮业务 revision 为 `f7a0196abde26d0adfe75ccbfa06d2a6f5303287`，更新器版本为 `1ca7174`，正式包 SHA-256 为 `f64398ae7aa10cf8e7ca3c2b2365f271e6dca34ab08d21a1336084f17320df3a`、32551 字节。发布范围只有 `backend/app/telemetry/config.py`、`worker.py`、`repository.py` 三个采集文件，不代表研究、DS 清理或其余父分支改动均已部署。本代理没有重新下载或计算发布包 hash，也没有可经现有只读 API 读取主机三文件源码/hash 的渠道；不能用包内 hash 冒充主机磁盘 hash。

### 安装回执与状态边界

只监看笔记本 `outputs/offline-win10-x64/target-install-status.json` 与 `target-install-events.jsonl` 的末尾及新事件，匹配上述 revision 和本次新时间，旧 10 月 29 日回执不作为成功依据。每段观察最多约 5 分钟，每次等待不超过 30 秒；未打印凭据。

- 本轮数据库备份达到 100.0%（1811843/1811843 pages，297 秒），复制 7078 MiB，结构检查通过。回执接收时间为笔记本 `10:54:23.609048`。
- 主机回执 `at=10:56:05.139887`、笔记本 `received_at=10:56:43.727159` 明确报告：collector 已用新 owner 连接，重启后两次已提交采样时间推进，最新提交为主机 `10:56:04.766`。
- 随后进入 `verifying`：对 7078 MiB 的备份执行完整内容校验，预算 4752 秒。本代理未干预该校验。
- 本轮观察最后一次读取返回于笔记本 `11:00:58`；最新已见回执为 `received_at=11:00:51.525374`、`at=11:00:12.932407`、`stage=verifying`，消息为 `Full snapshot content check running, 247s elapsed.`。截至该次观察没有本轮 `installed` 或 `failed` 回执。
- 随后又获授权进行下一段约 5 分钟的纯本地监看，未重复主机查询。该段最后返回于笔记本 `11:09:07`，最新已见回执为 `received_at=11:08:58.006008`、`at=11:08:19.393470`，仍为同 revision 的 `verifying`，消息为 `Full snapshot content check running, 734s elapsed.`。截至该次观察仍无本轮 `installed` / `failed`；完整内容校验结果仍待定，不能标为已认证的 verified 备份。
- 后续授权的一段约 5 分钟本地观察截至笔记本 `11:32:02`，两份回执文件未出现新事件；最新回执仍停留于上述 `11:08:58`、`verifying / 734s elapsed`。没有获得新的 `installed` / `failed` 或备份校验结果；仅凭这一旧回执不能确认更新器此刻仍在推进，也不能判断它已完成或失败。本段未再请求主机，未干预更新器。
- 用户随后反馈按 Esc 后，主机窗口继续显示 `2435s`、`2511s elapsed`。本地确实取得同 revision 的对应新回调：`at=11:36:41.088739 / received_at=11:37:19.784247` 为 2435 秒，`at=11:37:56.479212 / received_at=11:38:35.165044` 为 2511 秒，之后还有持续的新回调。这证明回调恢复，与用户反馈的时间顺序相符；未取得控制台 UI 选择模式的直接证据，不能断言此前停滞由文字选择导致。
- 按上述反馈再做一段约 5 分钟的纯本地观察，截至笔记本 `11:46:38`；最新回执为 `received_at=11:45:58.327270`、`at=11:45:19.623266`，仍是本轮 `verifying`，消息为 `Full snapshot content check running, 2954s elapsed.`。本段未见 `installed` / `failed`，没有重复生产查询或干预完整校验。2954 秒只是已运行时间，不是完成百分比；4752 秒预算也不能用来承诺完成时刻。备份完整内容校验及安装最终结果仍待回执确认。
- 随后获授权的 15 分钟纯本地观察截至笔记本 `12:08:44`，期间同 revision 回调持续更新，未再次出现明显停滞。最新回执为 `received_at=12:08:05.722742`、`at=12:07:26.961431`，仍为 `verifying`，消息为 `Full snapshot content check running, 4281s elapsed.`。本段未见 `installed` / `failed` 或明确的备份认证结果，未查询生产、未干预校验；最终安装和备份认证状态继续待核。4281 秒同样仅为已运行时间。
- 下一段授权观察中取得最终成功回执，均为本轮 revision：
  - 主机 `at=12:13:52.395322`、笔记本 `received_at=12:14:31.208876`：`Snapshot full content check passed (4667s).`，明确证明完整校验通过，`fullcheck=true`，不是预算超时后的未认证状态。
  - 主机 `at=12:13:53.567175`、笔记本 `received_at=12:14:32.354618`：`Completed snapshot checks registered; certified recovery points remain protected by retention policy.`。结合已审更新器 `register_retention_snapshot()` 的成功分支，此回执证明完整校验结果和备份认证已注册成功，可记 verified。
  - 主机 `at=12:13:53.598445`、笔记本 `received_at=12:14:32.385685`：`stage=installed`、`healthy=true`、`client=10.255.2.7`。备份目录为 `C:\Users\XMC\DshDensityControl\backups\source-update-20261102-104848-047320`。
  - 继续短时本地观察后，笔记本 `12:17:39.722` 直接核对状态文件和事件尾部，状态仍为上述 `installed / healthy=true`，同 revision 的最新事件仍为该成功回执；成功回执之后没有新事件，观察窗口内未见随后 rollback / failed。没有为此重复访问生产接口。

主代理在“新 owner 和两次提交已证明新服务启动”的条件下，明确允许先进行低成本运行核查。下列运行结果取自 10:59 前后，当时安装器尚未完成；随后最终安装完成和备份认证已由上列回执闭环。过程阶段的 `healthy=false` 不单独解释为采集失败。

### 更新后低成本实测

一次认证 `live-cache` 请求和三个顺序执行的限量只读 SQL 均成功，无超时、无重试：

| 检查 | 耗时 | 主机证据 |
|---|---:|---|
| `GET /api/v1/readonly-db/live-cache` | 531 ms | 主机 `checked_at=10:58:43.329`；笔记本收到于 `10:59:22.014`；`connected`、无错误、`poll_seconds=5`、`cadence_seconds=60` |
| 液位历史，时间索引、`LIMIT 12` | 78 ms | 已出现 `level-periodic / 30`，且与 `periodic / 60` 合并后连续推进 |
| 普通 403/MD_SET，时间索引、`LIMIT 12` | 31 ms | 3 条均为 `periodic / 60`，相邻间隔 60.156、60.093 秒 |
| 最新已存决策，时间索引、`LIMIT 1` | 32 ms | `sampled_at=10:58:10.109`，仍明确保存液位 lag90、lookback30 与 received_at 口径 |

历史查询起始时间为主机 `2026-11-02T02:56:00.000+00:00`。仍使用 `ix_telemetry_point_time`，每点最多 12 条；决策查询使用 `ix_decision_cycle_samples_sampled_at`、相同起始时间、最多 1 条。凭据由既有客户端内部加载，每次请求超时为 3 秒。没有调用 `/decision/current` 或 `/decision/hourly`，没有扫描全库或全点历史。

液位实际合并周期序列如下，均为原始接收时间；`alignment-evidence` 另外列出，不参与周期间隔判断：

| 记录 ID | 主机北京时间 `received_at` | 类型 | `cadence_seconds` |
|---:|---|---|---:|
| 4982390 | 10:56:04.766 | periodic | 60 |
| 4982410 | 10:56:35.250 | level-periodic | 30 |
| 4982419 | 10:57:04.922 | periodic | 60 |
| 4982439 | 10:57:34.906 | level-periodic | 30 |
| 4982449 | 10:58:05.015 | periodic | 60 |
| 4982469 | 10:58:30.360 | level-periodic | 30 |

前 5 条连续合并间隔为 `30.484, 29.672, 29.984, 30.109` 秒，已具有至少两组“全批周期 → 单点补充 → 下一全批周期”的连续推进。最新一段实际间隔为 **25.345 秒**，如实保留：实现按 30 秒时间桶选择原始约 5 秒采样，并不保证每个原始采样间隔精确 30.000 秒。不能要求 `level-periodic` 同类行自身每 30 秒出现，因为分钟边界由全批 `periodic` 提供液位记录。

另有记录 4982440，主机 `10:56:29.859`，仍为 `alignment-evidence / 5`；没有把它混入 30 秒周期序列。普通点 403/MD_SET 的三条周期时间为 `10:56:04.766, 10:57:04.922, 10:58:05.015`，均 `good`，未见本点 `level-periodic`。

运行缓存中最近 6 次液位采样为 `10:58:15.032, 10:58:20.048, 10:58:25.032, 10:58:30.360, 10:58:35.047, 10:58:40.047`，相邻采集间隔 `5.016, 4.984, 5.328, 4.687, 5.000` 秒，与 5 秒采集策略一致；入库补充没有把采集改成 30 秒。

更新后的已存决策主键为 `slot=358717658`，其 `input_timing`、液位 `alignment` 及该液位 `quality_sources.lag_seconds` 均为 90；`max_lookback_seconds=30`、`clock=received_at`、`selection=latest-at-or-before-target`。`prediction_at=10:58:10.109`、`target_at=10:56:40.109`，保存的 `offset_seconds=4.859466`。本轮未再次读取该决策的原始采样引用，不能把该偏差冒称另一项配置；此前时钟差记录继续保留，未校时。

本节运行验收范围为两个指定点、短时间窗口及一条已存决策；未证明所有点长期均无抖动。安装器最终完成、完整备份校验通过与认证注册成功已有明确回执；成功之后至笔记本 12:17:39 的本地观察窗口内未见回滚或失败。主机三文件磁盘 hash 仍未通过现有只读 API 直接读取，不以发布包 hash 代替该项证据。
