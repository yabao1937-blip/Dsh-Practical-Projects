# 脱粉 473/474 正式切换 RUNS（2026-11-03）

状态：**主机部署完成。准确整体 installed/healthy=true、用户成功确认及装后最低负载核验均通过。** 修订 trend-runs-20261103-642b8e234784；详见文末最终部署记录及 host-deployment-handoff-20261103.md。

## 精确范围

| 角色 | 473 | 474 |
| --- | --- | --- |
| 新活动输入 | `TIA_3_3.TIA_3_3.473/RUNS` | `TIA_3_3.TIA_3_3.474/RUNS` |
| 保留既有 key | `tag_881030838dd4d05ebcda` | `tag_04a29ab776cb07869148` |
| 退出活动配置 | `TIA_3_3.TIA_3_3.473/if_prod` | `TIA_3_3.TIA_3_3.474/if_prod` |
| 保留历史 key | `tag_3a82e49b2a019451078b` | `tag_caf82182c2980109b2de` |

NodeId 为相应 item 前加 `ns=2;s=`。来源固定 `kepserver-main`。只从活动配置移除两条精确旧定义；其他设备 `/if_prod`、连接配置、5 秒采集、普通历史 60 秒及液位 30 秒策略保留。RUNS 原配置对象字节不重新序列化，既有采集 key、类型、缩放、上下限及 definition_id 保持。

生产业务修改仅四个路径：

- `backend/app/telemetry/derived.py`：活动脱粉组合改为 RUNS，保留明确命名的 legacy 常量，旧点不是实时别名。
- `backend/app/hourly_brief/inputs.py`：主机写入一次 `settings.desliming_runs_cutover`，按精确 source、host effective_at、非 live 且非 realtime 门控，仅为切换之前的历史请求重新注册两条精确旧 disabled 元数据。
- `backend/app/services/decision_sources.py`：默认/显式组合均走时态成员解析；新输入契约始终记录 `input_derivations.desliming.version=desliming-runs-v1`，包括输入缺失时，防止无样本状态沿用旧 context hash。现有模型、质量、同批、时效和真实配置变化保护保留。
- `backend/app/decision_hourly/history.py`：旧小时按旧成员读历史；跨切换小时或同一路定义混合时暂不混合 if_prod 与 RUNS 的运行占比，原存档快照不改写。

本地历史脚本 `backend/scripts/audit_gpt_database_samples.py` 固定引用 legacy 常量；不将该脚本或未追踪的 EXPERT8.1 研究文件装入生产。`join_gpt_recent_assays.py` 中的原始历史身份保留。冻结 EXPERT8 / GPT v2 模型、系数、训练/研究产物、原始化验、历史档案、共用数据库表与历史标签均未修改。

## 主机事务与恢复

新组件 `backend/scripts/host_runs_switch/switch_runs.py` 支持 `--install-dir ROOT --dry-run` 零写预检与正常 apply；组合入口追加 `--component`，组件成功只上报 `copying / healthy=false` 并标明 `[RUNS component]`，由顶层入口在两个组件均成功后才上报整体 installed。

预检验证有限源码基线、全部包成员哈希、已装两条 RUNS 定义、原精确点身份、活动 collector owner/lease/connected、最近 Good Boolean RUNS、原始同批/同接收时刻。compact-v1 point metadata 通过只读 Catalog 解码；保留原 SQL JSON 字节作变更对照。读取仅为小表主键/最新 revision，以及 source+point+received_at 索引 LIMIT 1，不触发生产全表扫描、`decision/current` 或 `decision/hourly`。

备份精确原配置、四个源文件和少量受影响 metadata journal；不复制约 7GB 数据库。仅停本安装的 Web 和 collector，等待自然租约到期，不清 lease、owner 或普通 spool。Web helper 使用 Windows 真实 argv、进程 CWD、创建时间及保持打开的进程句柄精确核对目标 `backend/serve.py`，检查所有 8000 listener；其他安装或不能证明归属的监听不停止。

配置原子移除旧两条，数据库一个短事务只新增 cutover setting、必要时 append 新来源 revision、将旧两条 enabled 置 false。只有当前 `choices.desliming` 为精确来源与旧 473/474 key 时迁移为 `desliming_status`；空选择、默认选择、其他 choices、目标范围和旧 revisions 保留。新旧 point.config/latest/readings 不删除、不改名。重启须确认新 owner、未来 lease 和两次提交时间增长，之后验证 fresh Good Boolean RUNS 的 key/node/definition/status/同批身份，再启动 Web 并做轻量 health。Web 启动完成后再次检查活动 owner/未来 lease/新鲜提交和 Good 同批 RUNS，防止 collector 在 Web 启动窗口退出仍被报为 installed。

异常回滚只恢复本组件源文件、配置和 metadata 变化，不覆盖第三方编辑；发现外部配置/源码/来源 revision 漂移时拒绝盲目覆盖或重启。安装成功后重复运行不重置 cutover、不新增 revision，仍须验证活动采集及 Good RUNS。断电后的未完成 journal 需保留用于恢复诊断，不宣称异常断电具有跨文件与 SQLite 的单一原子事务。

## 可信源码基线与无关压滤机保留

现有只读桥接不支持任意源码或源码 hash 读取，与此前液位部署报告一致。**没有遥测过这四个主机源码的实际磁盘 hash。** 本地主机预检将用有限可信旧 hash 比较，未知版本拒绝。

Inputs、decision_sources、history 在可信 `6dd6411` 与当前 HEAD 逐字节一致。derived 在 `6dd6411` 仍保留 A416/A417 channel aliases，`1964bbe` 已恢复 TIA_4_2 常量。为避免 RUNS 更新顺带改变压滤机口径，包中包含两条明确派生变体：`variants/derived-6dd6411.py`、`variants/derived-6dd6411-crlf.py`。它们仅替换 DESLIMING 常量区，其他原始 LF/CRLF 字节（包括 PRESS aliases）完全保持；精确旧 hash 及该变体的新 hash 选择同一分支，保证重复安装不会改为其他分支。主 payload 对应 `1964bbe`。各可信基线显式枚举 LF/CRLF hash，运行时不对未知源码做模糊规范化接受。

## 验证与候选

- 专属测试 `test_desliming_runs_switch.py` 最终 31 项、原 RUNS 追加 33 项、Windows 原生 Web 归属 5 项，最终联合 **69 passed in 47.05s**：实时卡片/业务/八因子输入、历史 source/time/live/realtime 门控、missing context 分离、跨切换小时、原配置与各点字节、compact 元数据、零写预检、成功/幂等、各阶段失败原样恢复、坏质量/类型/定义/批次拒绝、外部编辑拒绝、LF/CRLF legacy 变体的预检/安装/重复/回滚，以及 Web 启动窗口 collector 退出后阻止 installed。
- 既有 EXPERT8、derived、decision_sources、observation_lock、point metadata、hourly brief 与当时专属测试组合 **180 passed in 138.99s**。此项包含部分专属测试，不与前项相加计算独立测试数；后续新增边界只重跑相关专属测试。
- 原 RUNS 追加测试与当时专属测试合计 **58 passed in 21.68s**；原追加测试单独部分为 33 项。更新其中一项旧预期，使其核验用户现已确认的 RUNS 业务输入，同时检查旧 if_prod 原值不改写。
- `test_runs_switch_web_ownership.py` Windows 真实临时进程验证：相对/绝对 owned 与 foreign 路径四种场景；另真实 owned serve.py 首 listener 加 foreign 次 listener，拒绝停止且 owned 保持存活，共 5 项通过。无生产进程参与。
- Web helper 封存 SHA-256：`28173f157366e76f5b5a02f95def5c8a29c9b4eb1763821a7ee522481fd2ae52`，已交前端组件复用。
- 独立代理对 v3 实际 ZIP 验收 **42 passed**，包含 primary、6dd LF、6dd CRLF 三基线的真实封装模块 success/repeat 与 post-Web collector-loss 完整回滚共 6 场景；活动点少两条、无旧活动 key、RUNS 组合 Good，历史、原 metadata、无关 setting 和普通 spool 保留；变体保留 PRESS 区外原始字节。

最终候选：`output/runs-switch-20261103-candidate-v3/SwitchDeslimingRuns.zip`，**55,624 字节**，SHA-256 **`8eb3650f51e84c135a078043cec455fb61fa270ac1f738ff8d9a7702960d3ba7`**。13 个精确成员为 manifest、六个工具/说明、四个 runtime 源文件和两条 derived 变体。无全量配置、数据库、模型或秘密配置；包中 status 回调地址仅由本机既有门户设置生成，公开报告已脱敏。CMD 包内规范 CRLF。v1/v2 仅保留为候选审计，不发布；v3 相对 v2 只补 Web health 后的 collector/RUNS 复核。

集成策略已由主代理批准：顶层一个 CMD，先两个组件都预检，再顺序执行 RUNS 独立事务与三文件趋势前端独立事务，按组件报告成功/失败；不合并为假想的单一全库事务，不允许部分成功上报整体 installed。本次主机只执行到了拒绝的预检，未进入 RUNS 或前端 apply。

## 主机零写预检拒绝与兼容诊断

用户回传：在 `C:\Users\XMC\Downloads\ProjectUpdate (30)` 执行顶层入口，错误为 `RUNS failed: Unknown installed source version: backend/app/telemetry/derived.py; no file was overwritten`。顶层协调器因此未应用任何组件，也未停服务或写生产配置/数据库。

已核对封存 v3：primary/1964bbe 的 LF 和 CRLF 都列为有限基线（`9ba2dc2929e29830d79d1243aea4bdfeaa8a567d2dac9d0788c6e8407edc3b3b`、`7aed7d95c62d82f80c0db3eee994f77d15a1dad95db5825fe6f7674825d2a5af`），6dd LF/CRLF 各有对应变体。不能将本次失败归因为漏掉 primary CRLF。UTF-8 BOM 未列，但在实际字节到达前不据猜测扩充。

本机最早完整安装底稿及 QA 副本的 derived.py 均为 8,130 字节、131 个 CRLF、无 BOM，SHA-256 `1a00abe3e7b0278baffa2cd3052f401381fc4be8d3073fe9278d25e6d6dea34e`，逐字节等于 `651f37b` 的 canonical CRLF；这是更早交付的候选来源，**不是主机当前磁盘 hash 的实测结论**。最新已有 host-baseline hash 文件仅包含前端三文件，尚无后端四文件的实际源码证据。

最小只读采集限定四条 runtime 源文件的原始字节、SHA-256、长度、BOM/行尾统计，不读取配置、数据库、模型、环境变量，不递归目录，不停止或重启服务。取得实际四文件后，先逐字节与封存来源比对、重现 v3 拒绝，再决定精确有限变体和独立的预检/成功/重复/回滚验证；未知源始终拒绝覆盖。

主代理另要求后续未知源错误自带小型安全诊断：相对路径、实际 SHA-256、字节数、UTF-8 BOM、CRLF/单 LF/单 CR 数量。错误消息及失败回调只携带这些元数据，不打印源码内容；两项相关回归通过（`2 passed, 30 deselected in 2.71s`），原未知源码仍被拒绝且未改写。这一诊断增强已纳入后续 v4，不修改已封存 v3。

## 实际源码兼容修正 v4

用户执行只读四文件采集后，原回执已验证并落盘：`output/runtime-source-capture-20261103/received/capture-0a5d51cbb52075ee/capture-report.json`，23,446 字节，SHA-256 `0a5d51cbb52075ee5e2b1ddf683bbe7825ec60cda79e9571eafcf95e56a68a51`。采集主机根为 `C:/Users/XMC/DshDensityControl`，客户端时间 11:02:38+08、接收 11:03:10+08。发布代理按固定路径、时间、root、解压上限、EOF、SHA、字节数和行尾逐项验证；独立代理另审原回执，证据为 `output/runs-switch-review-20261103/actual-host-source-capture-audit.json`。

| 实际源文件 | 字节 | SHA-256 | 行尾 |
| --- | ---: | --- | --- |
| `backend/app/telemetry/derived.py` | 8,130 | `1a00abe3e7b0278baffa2cd3052f401381fc4be8d3073fe9278d25e6d6dea34e` | 131 CRLF，无 BOM |
| `backend/app/hourly_brief/inputs.py` | 18,861 | `1c03e3589ab56c7e75367ee1895ef83f8e95df5d51acda943c6be2bc6cbaa819` | 311 LF，无 BOM |
| `backend/app/services/decision_sources.py` | 13,168 | `780c3a57e2dba3c09e5473c3ac9a40a8c138a6f50d691aa80344d5ecfdab65e4` | 221 LF，无 BOM |
| `backend/app/decision_hourly/history.py` | 10,994 | `6c498be210e4fa1dcd2a2c50b913b9915523815dfd3daaab1a6daca5e22cfe9f` | 176 LF，无 BOM |

根因现已确定：derived 是 651f37b 的 CRLF 原始版本，未含后来的空 PRESS alias 常量；Inputs 未含 alias import/注册循环；另外两份与既有可信基线逐字节一致。原 v3 用“可信较新源码”代替实际线上基线，因此无法识别前两份。原拒绝保护正确生效；没有将实际旧 hash 直接加入主 payload 白名单。

v4 新增两条精确变体：

- `variants/derived-host-1a00abe3.py`：只替换现场 DESLIMING 常量段，CRLF、其余原字节和原有 PRESS 定义完全保留，不补 PRESS alias 常量。
- `variants/inputs-host-1c03e358.py`：只替换相关 import、在原 cutoff 后插入门控、在原点注册循环后插入精确历史点注册、替换 group 入口加入成员解析，共四个明确字节区段。没有添加任何原线上不存在的 PRESS alias import/循环。

这两条按精确旧/新 hash 成对选择：旧旧或新新可用；仅选中一条、与 primary/6dd 族混搭、或只有其中一条更新时，均在停服务前零写拒绝，防止独立“已知 hash”混配导致 Python import 失败。原 primary/6dd 分支及其重复、回滚行为保留。未知源码仍拒绝，新增一个未审核字节也不能通过。

构建器把原始四文件 SHA、回执 SHA 和每个编辑段的 offset、增删长度及增删 SHA 写入 `output/runs-switch-20261103-candidate-v4/host-source-compatibility-proof.json`；据此独立核对所有区段外的原字节保持。采回的原源码不直接执行，修改后的受审变体在隔离测试中验证实际 Python 导入与业务输入。

v4 最终包：`output/runs-switch-20261103-candidate-v4/SwitchDeslimingRuns.zip`，**65,654 字节**，SHA-256 **`3d98f9cb7b99ca5c6593100f405027365c499f94b0fb3cef20d2bade1af7d659`**，15 个精确成员（相对 v3 只新增上述两个 source variants；matcher、README 和 manifest 更新，其余源/工具原字节不变）。版本为 `desliming-runs-switch-20261103-v4`。

最终实包定向验证 **16 passed, 26 deselected in 35.13s**：实际四源旧 v3 拒绝复现、v4 零写预检/正常/重复/完整回滚、Web 启动后 collector 退出不报 installed、四种不兼容配对拒绝、单字节未知改动拒绝且安全诊断、逐编辑段外字节保持、实际变体导入/live RUNS/历史 if_prod 身份，以及原 primary/6dd 分支相关预检/成功/重复/回滚。没有重跑未改变的全套共用业务测试，也没有写生产主机。


## 2026-11-03 最终主机部署记录

2026-11-03 13:19:23 主机整体 trend-runs-20261103-642b8e234784 回传 installed/healthy=true（13:19:56 门户收到），用户确认 UPDATE INSTALLED。装后 health200、三前端 SHA 全匹配；collector 新 owner connected/未来 lease，后续 SQL last_sample_at 从05:19:02.819Z推进到05:20:03.031Z；RUNS两点Good Boolean同批推进，旧两点disabled且活动批次不再含旧点；cutover05:18:15.125Z，来源revision9及原显式desliming_status绑定/其它choices/目标范围保持。5/60秒缓存实测；30秒配置及四源hash以受审安装器精确校验为证据，现有桥接不能独立读回。完整交接与限制见 host-deployment-handoff-20261103.md；EX8.1及其他研究保持暂停，笔记本8000未启动。
