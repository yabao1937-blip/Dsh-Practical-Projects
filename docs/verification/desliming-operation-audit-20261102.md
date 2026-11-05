# 脱粉473/474点位只读核查（2026-11-02）

本轮结论：用户指定的两个 `RUNS` 节点均可只读访问，类型为 Boolean，本次瞬时读数均为 `false`、质量 Good。现有旧 `if_prod` 的部分记录不能证明脱粉设备自10月16日至今没有开启。用户指出可能选错点位后，旧点位全期抓取停止，未完成全期统计；不提供全期运行次数、时长或“从未运行”的结论。

## 新RUNS节点

采用既有本机只读工具的连接配置，端点 `opc.tcp://10.255.1.21:49320`，既有匿名/None配置保持不变。仅建立一次连接、发出一次 Read 请求，对以下两节点分别读取 DataType 和 Value，共4个属性；不浏览地址空间、不写节点、不修改任何采集配置。

| 项目 | 473 | 474 |
|---|---|---|
| NodeId | `ns=2;s=TIA_3_3.TIA_3_3.473/RUNS` | `ns=2;s=TIA_3_3.TIA_3_3.474/RUNS` |
| 稳定point key | `tag_881030838dd4d05ebcda` | `tag_04a29ab776cb07869148` |
| DataType属性 | `ns=0;i=1`（响应字符串 `i=1`） | 同左 |
| Value VariantType / Python类型 | Boolean / bool | Boolean / bool |
| 原始Value | `false` | `false` |
| DataType与Value状态 | Good / `0x00000000` | Good / `0x00000000` |
| Value SourceTimestamp（UTC） | 2026-11-02 07:52:13.111 | 同左 |
| Value ServerTimestamp（UTC） | 2026-11-02 07:52:13.111 | 同左 |
| 笔记本接收时刻（北京时间） | 2026-11-02 15:53:02.520 | 同左 |

连接与读取耗时94毫秒，每阶段超时界限3秒，没有重试。笔记本接收时间与KEP时间分属不同机器；未独立验证时钟同步，不能把两者49.409秒差直接解释为数据延迟或过期，也未进行校时。DataType属性的ServerTimestamp为UTC 07:52:13.095，SourceTimestamp未提供。

当前主机配置表按两个标准key的精确主键查询返回0行（31毫秒，笔记本北京时间15:46:49.087）。这证明本次查询时两个标准key尚未配置；未扩查别名，也未查询它们的历史。本轮没有取得RUNS从10月16日至今的连续历史。点位实施由另一任务负责，本核查未修改主机。Good只表示本次OPC读取状态，节点与真实设备运行的对应关系仍需现场操作对照。

原始证据：[runs-db-check.json](../../output/desliming-operation-audit-20261102/runs-db-check.json)、[runs-direct-probe.json](../../output/desliming-operation-audit-20261102/runs-direct-probe.json)。只读脚本：[probe_runs_readonly.py](../../output/desliming-operation-audit-20261102/probe_runs_readonly.py)。

## 旧if_prod点位的瞬时与部分历史证据

主机检查时刻为北京时间15:26:49.615，采集状态connected、owner及lease有效，poll=5秒、普通history=60秒。两点样本均为主机UTC 07:26:48.475收到，按主机同一时钟计算年龄1.140秒，原始值和规范值均为Boolean `false`、quality=good、status_code=0、issue为空。

| 旧点位 | point key | 当前definition id |
|---|---|---|
| `ns=2;s=TIA_3_3.TIA_3_3.473/if_prod` | `tag_3a82e49b2a019451078b` | `28595b25eb48567136abb134e5230d1518010851625c537ac061a1689305d393` |
| `ns=2;s=TIA_3_3.TIA_3_3.474/if_prod` | `tag_caf82182c2980109b2de` | `28f5e5bd434bc80322128596b8af377b308ea3608f91a549e8cb1942e498c1fa` |

当前两旧点配置均为boolean、scale=1、offset=0、max_age_seconds=30。规范化代码仅接受Python bool作为有效布尔值，数字或字符串不会被直接转成布尔值。本次已核当前配置及definition，没有完成所有历史点表版本的逐版映射审计，不能声称全期语义完全不变。

历史读取先对两点EXPLAIN，均命中 `ix_telemetry_point_time (source_id, point_key, received_at)`，未全表扫描；每点按北京时间日界分段，串行、单请求3秒超时、LIMIT 801（800条处理加1条lookahead）。首日试页分别610/156毫秒。分页键为 `(received_at,id)`，保留原始sample、id、batch_id、查询时间界及receipt；查询不筛true，不把缺失当false。

读取在99.469秒时遇到首个 `readonly_transport_unavailable` 后自动停止，没有重试，进程已正常结束。此后用户指定新RUNS节点，进一步明确不再继续旧点位全期分页。最终状态 `stopped_no_retry`，不是complete：

| 范围 | 473 | 474 |
|---|---:|---:|
| 处理的唯一记录 | 58,847 | 50,847 |
| 已完成日查询窗口 | 10月16、17、18日 | 10月16、17、18日 |
| 其后范围 | 10月19日部分 | 未读取 |
| 读取器发现的有效true记录 | 0 | 0 |
| 读取器发现的不合格bool记录 | 0 | 0 |

以上计数为读取器处理的109,694条旧点位记录，不能外推到未覆盖日期、采集缺口或真实设备运行。不同日期/点位使用各自短读快照，不是整个期间同一事务；极晚补录旧时间戳不在绝对完整性保证内。密集存储记录也不等于设备重复启动次数。本轮不对部分数据计算物理运行时长，未覆盖或缺失时段保持未知。

独立复核只校验了两点首页各800条及下一页lookahead，SHA、排序、时间窗和状态严重位通过，均为false/good；它没有认证全部140页汇总、全期或时长。参见[partial-review.json](../../output/desliming-operation-audit-20261102/review/partial-review.json)。

## 证据与边界

- [preflight.json](../../output/desliming-operation-audit-20261102/preflight.json)：旧点配置、瞬时样本、索引计划与轻量查询耗时。
- [acquisition-result.json](../../output/desliming-operation-audit-20261102/acquisition-result.json)：停止状态、页数、读取器汇总及已完成窗口；最终host截止为空，不能宣称覆盖导出结束时刻。
- `output/desliming-operation-audit-20261102/raw/`：140页gzip JSON；每页为含原始rows数组的对象，801条响应的最后一条只作lookahead，不能重复计入处理数。
- `output/desliming-operation-audit-20261102/receipts/`：每页SQL边界、耗时、处理数与原始数据SHA；[read_desliming_history.py](../../output/desliming-operation-audit-20261102/read_desliming_history.py)仅为已停止审计的读取器，不再运行。

没有读取本任务之外的化验值，没有修改生产数据库、PLC、服务或时钟。旧粗精煤泥样本中的脱粉特征仍是当时旧 `if_prod` 观测，不能用今天的RUNS瞬时值回填历史，也不能把样本切片全为0解释成设备全期停机。
