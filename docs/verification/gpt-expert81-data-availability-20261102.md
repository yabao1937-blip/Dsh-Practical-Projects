# EXPERT8.1 数据可用时刻元数据审计（2026-11-02）

本报告记录手工化验存储、修订机制、10 月 31 日冻结快照，以及后续分阶段获批的数据读取。第一阶段生产记录元数据检查完成于笔记本北京时间 **2026-11-02 13:10:31.862**，当时不读新增灰分或特征；随后先封存特征、锁定预测，再获批单独解封 6 条标签，最后另获扩大当前训练信息的明确授权。**当前已独立捕获 ID≤202、排除197的76条当前值；197及ID>202的值未读取。191虽原值已知，仍按stopped不训练。** 当前统一值快照为北京时间 **14:42:18.593**，没有覆盖旧版本或历史成绩。各阶段没有改数据库、时间、模型或生产参数，没有创建实验脚本，没有读取 `request_hash` 或凭据内容。

## 结论与正式可用性规则

当前最终灰分值不能凭 `created_at` 证明在该时刻已经是相同版本。主机表没有 `updated_at`、`version` 或 `revision` 字段，目标表没有 trigger；已审 API 允许原地修改 `metric/value`，保留最初 `created_at`，也允许硬删除。未找到可据以重建化验值版本和修订可用时刻的审计证据。

在缺少修订证据时，正式 `available_at` 采用**首次包含该版本值及身份、时间证据的完整快照捕获时刻**，不把当前最终值倒灌至原 `created_at`。旧冻结 68 条的已保存版本只确证在原 `captured_at` 已知；第一阶段元数据读取不构成新增值的完整捕获。后续 6 条、182 及当前 76 条的独立捕获分别记在第三、第四阶段，不能反推为最初录入时的已知版本。当前76视图统一采用新捕获时刻进行保守重封，旧版本和旧可用性证据仍保留在历史视图。

用户授权的例子保持明确：13:00 填入 12:00 的样本，可在结果到达后评分 12:00 的事前预测，校准只影响其实际可用时刻之后的预测。若最终记录后来被编辑，不能将编辑后的值按最初 13:00 的 `created_at` 回填。评分目标时刻、结果可用时刻、预测生成时刻分别保存；严格时间比较还须处于可比的时钟域。

解封前“本任务未查看新增值”只证明**当时本任务保留集未解封**，不证明这些值从未被其他历史会话、界面或流程看过，不因此称作全局独立盲测。各阶段实际读取的标签均属于本任务已看数据，后续不能继续称作未见独立新样本；当前曝光范围为上述76条，不含197及ID>202。

## 冻结基线

来源为 `output/gpt-factor-phase1-20261102/input_audit.json`，仅复用已有冻结工件，不重新导出覆盖：

| 元数据 | 冻结值 |
|---|---|
| schema | `gpt-assay-audit-v1` |
| captured_at | `2026-10-31T03:03:29.887+00:00`，北京时间 10 月 31 日 11:03:29.887 |
| rows | 68 |
| max_assay_id | 179 |
| dataset_sha256 | `50f0d98373163fc07ff3bb7fa90a4ebcc14ac685008ca41f0c72921da1cd41a9` |

冻结工件本身已写明 `created_at is initial entry time, not report/revision availability`。本次没有把该限制放宽。

`backend/scripts/audit_gpt_database_samples.py:246` 使用执行审计进程的 `datetime.now(timezone.utc)` 生成 `captured_at`；它不是从化验行 `created_at` 推导，也没有在此处读取主机当前时刻。应保留 `capture-process-clock` 语义，不将其当成已确认同步的生产时钟。此前另有两机约 38—39 秒时间差的证据，见 `docs/verification/production-timing-readonly-20261102.md`；不能用今天的差值回溯校正 10 月 31 日，或宣称现场已校时。此次新增记录距冻结捕获均为数小时，新增计数不依赖秒级时钟对齐。

## 存储和编辑语义

本地源码与生产目标表 schema 共同核对：

| 证据 | 已核实行为 |
|---|---|
| `backend/app/models.py:97—120` | 表为 `manual_measurements`；有 `observed_at/created_at/time_basis`，没有化验版本或修订时间字段。类注释的 Append-only 不能替代实际 API 行为。 |
| `backend/app/routers/manual_measurements.py:99—105` | 创建时以服务器当前时刻填写 `created_at`，`observed_at` 为提交的采样时间或当前时间。 |
| 同文件 `116—138` | PATCH 原地改 `metric/name/unit/value`；保留 `observed_at/created_at`，没有写化验修订历史。 |
| 同文件 `132—135` | `request_hash` 保留原 POST 的幂等语义，不随编辑更新，不能当作当前版本审计。本任务没有查询该哈希。 |
| 同文件 `141—149` | DELETE 为硬删除，无 tombstone。 |
| `backend/app/services/manual_measurements.py:38—50` | 现有 `created_at <= cutoff` 过滤仍可能读到原地编辑后的当前最终行，不能据此证明严格的历史版本可用性。 |
| 主机精确表 DDL 和索引元数据 | 实际列为 `id, metric, name, value, unit, observed_at, created_at, client_id, request_hash, time_basis`；无 `updated_at/version/revision`；该表无 trigger。 |

主机确认存在 `ix_manual_measurement_metric_id(metric,id)`，以及 `ix_manual_measurement_effective_latest` 与 client_id 唯一索引。目标表 DDL 未声明 SQLite `AUTOINCREMENT`；因此不能仅凭最大 ID 推断不可删除、不可复用的事件历史。本次同时比对冻结成员及时间元数据，仍不能识别只有值发生变化的修订。

以上结论针对已审应用代码和目标表 schema；没有取得外部修订审计、报告发布日志或其他能证明历史版本可用时刻的证据。

## 当前元数据与冻结集比较

| 项目 | 结果 |
|---|---:|
| 当前 `coarse_slime_ash` 记录数 | 77 |
| 当前该 metric 最大 ID | 202 |
| 冻结集记录数 | 68 |
| 相对冻结集新增成员 | 9 |
| ID 大于冻结上限 179 | 9 |
| `created_at` 晚于冻结 `captured_at` | 9 |
| 冻结 ID 当前缺失 | 0 |
| 冻结 ID 的 `observed_at/created_at/time_basis` 元数据差异 | 0 |
| 可识别的 value-only 修订次数 | **未知；没有修订证据，未读取或比较 value** |

“旧 68 条元数据相同”不等于“旧 68 条值从未修改”。9 条新增成员也不能证明所有修订事件总数只有 9 个。

用户最新明确要求忽略时间错误的 **ID 197**。本轮训练和验证均排除该记录，保留原数据库行、原时间和排除理由，不改时、不删除。在第一阶段，其余 **8 条仅为后续时间、输入覆盖和协议审核的元数据候选**，不是仅凭这些元数据就可训练或已确认独立检验的标签。随后的输入资格、预测锁定及授权解封范围分别记在后续各阶段。

下表为这 8 条的原始 UTC 时间；全部 `time_basis=production-time-v1`，没有灰分值或新特征：

| ID | observed_at UTC | created_at UTC |
|---:|---|---|
| 182 | 2026-10-31T00:19:05.000+00:00 | 2026-10-31T05:19:36.653+00:00 |
| 185 | 2026-10-31T16:51:52.000+00:00 | 2026-10-31T19:05:50.078+00:00 |
| 187 | 2026-10-31T20:35:52.000+00:00 | 2026-10-31T22:21:46.732+00:00 |
| 190 | 2026-10-31T23:31:26.000+00:00 | 2026-11-01T02:16:04.027+00:00 |
| 191 | 2026-11-01T01:39:00.000+00:00 | 2026-11-01T03:26:43.626+00:00 |
| 195 | 2026-11-01T13:32:44.000+00:00 | 2026-11-01T15:09:34.650+00:00 |
| 200 | 2026-11-02T00:23:06.000+00:00 | 2026-11-02T02:04:50.588+00:00 |
| 202 | 2026-11-02T02:38:50.000+00:00 | 2026-11-02T04:11:37.961+00:00 |

ID 182 的采样时间早于冻结捕获，而首次录入晚于冻结捕获，直接说明不能仅按 `observed_at` 划分“当时已经有标签”。

排除记录 ID 197 的原始元数据为 `observed_at=2026-11-02T15:56:45.000+00:00`、`created_at=2026-11-01T20:48:57.684+00:00`，即北京时间 11 月 2 日 23:56:45 与同日 04:48:57.684。采样时间晚于录入时间，也晚于本次检查时间；排除依据是用户对该错误时间的明确指示，不涉及灰分值或预测误差。

## 已批准生产查询边界

查询计划先提交主代理，并获明确批准。既有 `remote_mode.settings()` 内部加载凭据，使用 `RemoteConnection(settings(), timeout=3)`；凭据未打印、未写文档、未传命令行或外网。所有生产操作均为只读桥接 SELECT，按顺序执行，快照完成后释放。

1. 仅按 `tbl_name='manual_measurements'` 和 `type IN ('table','index','trigger')` 读取 schema，`LIMIT 16`。索引明确核实；无 trigger。首轮逐行列解析漏掉 ALTER 追加到同行的 `time_basis`，因此保守停止，未执行任何记录查询。主代理批准一次精确表 DDL 重取，随后只在临时内存 SQLite 解析该 DDL，完整确认列；这是本地 parser 修正，没有向生产发 DDL/DML，也没有重试失败网络请求。
2. 用 `ix_manual_measurement_metric_id`、固定 `metric='coarse_slime_ash'`、`ORDER BY id DESC LIMIT 1` 取得上限 202；未超过已批准的冻结上限加 2000 的停止阈值。
3. 在同一 metric 与 `1 <= id <= 202` 的固定索引范围计数，结果 77，小于已批准的 200 行上限。随后只 SELECT 身份和时间元数据，`LIMIT 201`，实际 77 条；计数与行数一致，未翻页或扩大范围。

实际数据列查询如下，**没有 `value`、`request_hash` 或 features**：

```sql
SELECT id, metric, observed_at, created_at, time_basis
FROM manual_measurements INDEXED BY ix_manual_measurement_metric_id
WHERE metric = ? AND id >= ? AND id <= ?
ORDER BY id LIMIT 201;
```

精确 DDL 重取 78 ms，计数 32 ms，元数据行查询 15 ms；未遇到数据库繁忙或网络超时，没有重试堆积。没有调用会返回化验值的通用列表/latest HTTP 接口，没有读取新增标签的原始值、哈希或特征，没有取全库、宽时间历史或启动新采集。

## 交接

上述可用性规则、8 条候选的 sample/created 元数据、ID 197 排除决定与时钟域边界已同步主代理、`expert81_research_max` 和 `expert81_runtime_max`。首次交接时新值尚未解封，后续仅按末节范围另获授权；不以 `created_at` 代替已证明的版本可用时刻。本代理没有训练或激活模型。

## 第二阶段保留输入查询计划及执行回执（先于标签解封）

本节是随后独立获得主代理批准的 features-only 阶段。计划先保存至 `output/gpt-expert81-holdout-20261102/feature-query-plan.json`，再执行已批准的四条 SQL；不改 phase1/phase2 冻结工件。研究候选在读保留特征前已经冻结为 C7：协议 SHA `79afa9387be716b65a00a7cd672d8d8b7664a503a1db1cbb403407d151869374`，核心 SHA `2a92112eef2cf2460c9d4923c6a18e2905b2dc838a9c396eebfd6d8ce3b44a98`，研究脚本 SHA `795f5a3489ddd3ed72b9a054eae36c0d0388db6bf61b7648eef3b8403a98de9e`。候选内部 artifact hash 为 `8c5d2a54225d460e082dd7c8e7858bd19433ae87d7ccd2a8637d040b44fbbf71`，候选 JSON 文件 SHA 为 `ee03b8ea7a701a498715cc9b413ac03136fe1412a0207229ad7d39415a208e64`。

ID 182 的 sample_at 早于正式旧种子 capture，不查其特征，保留 `seed_not_available_at_sample` 覆盖拒绝；197 按用户决定不查特征并排除本轮训练/验证。只对其余 7 个 ID 读取八因子证据：`coal_amount, sysA, sysB, sys401, sys402, desliming473, desliming474, level`。

### 固定规则与预算

- 液位只取 `t-90s` 及此前 30 秒的最后一条原始接收记录，即 `[t-120s,t-90s]`。其余七因子沿旧审计查 `[t-120s,t]` 的最后一条；不会跳过较新的坏记录寻找更旧好值，不插值、不取未来值。
- 复用 `audit_gpt_database_samples.py` 的 definition、quality/issue、类型、煤量大于 10、液位 0—100、source freshness 和 pairing 规则。严格层要求八因子均 `good`，且非液位七因子同 source/batch/received_at；非液位年龄 31—65 秒仅记 `minute_approximation`，不晋升严格层。
- 沿用的稳定/一致性证据是同批次、定义和质量；原审计没有额外 N 秒开关稳定窗口或液位波动阈值，本轮没有临时增加这种筛选，也不据此宣称全过程物理稳定。
- 本轮只固定 90 秒 lag，不查询 60/120 秒。旧训练 34 条的 frozen common-valid cohort 保持不变；不因该旧训练定义而对新保留样本补查其他 lag，也不声称本轮新样本满足三 lag common 条件。
- `invalid_label` 和邻近同值/冲突值等依赖灰分的检查仍待解封；不能在本阶段把 `feature_eligibility` 冒称最终标签资格，也未按标签值或预测误差筛除。
- 特征阶段最多 4 条 SQL，最多返回 7 个 as-of 来源版本、8 个白名单点配置、56 个点时刻观测。历史逐点子查询均强制 `ix_telemetry_point_time`、声明的窄窗口与 `LIMIT 1`；来源版本使用 `effective_at` 索引，点配置使用复合主键；没有全点枚举。
- 固定元数据若需解包，只允许既有 immutable point-metadata GET，去重缓存且最多 24 次；每请求 3 秒超时、串行、零重试，不读冷档、不翻页，不扩 source/window。预测冻结且再次获主代理明确授权后，标签阶段才允许额外最多 1 条仅标签/元数据 SQL，总 SQL 上限为 5；第二阶段结束时第 5 条尚未执行，后续执行另记第三阶段。成功路径最多 29 次数据读取 HTTP 加 4 次只读事务开关；失败只允许必要清理，不新增读查询。

### 实际执行

特征证据捕获时间为 `2026-11-02T05:38:40.782+00:00`，属于 capture-process-clock，**不是新灰分完整快照时间**。本次总耗时 672 ms，4 条 SQL 成功，固定元数据 GET 实际为 0，无超时、无重试、无额外生产查询：

| SQL | 耗时 | 返回行数 | 上限 |
|---|---:|---:|---:|
| 精确 schema / index 预检 | 62 ms | 6 | 6 |
| 7 个 sample 的 as-of source revision | 31 ms | 7 | 7 |
| 8 个已知点的配置 | 32 ms | 8 | 8 |
| 7×8 窄窗最后观测 | 515 ms | 56 | 56 |

7 条的六开关组均为 `0,1,1,1,0,0`。严格层 2 条、分钟近似层 4 条、停煤资格拒绝 1 条；没有放宽质量门槛：

| ID | feature_eligibility | 非液位七因子接收年龄 | 液位相对 t-90 的接收年龄 | 原规则判定 |
|---:|---|---:|---:|---|
| 185 | minute_approximation | 49.590 秒 | 19.672 秒 | 近似层单列 |
| 187 | minute_approximation | 49.073 秒 | 19.262 秒 | 近似层单列 |
| 190 | strict | 22.519 秒 | 22.594 秒 | 可交锁定预测 |
| 191 | incomplete | 57.512 秒 | 27.213 秒 | `coal_amount` 按原大于 10 的规则记 stopped；不是缺少液位 |
| 195 | minute_approximation | 41.609 秒 | 11.765 秒 | 近似层单列 |
| 200 | strict | 1.528 秒 | 1.858 秒 | 可交锁定预测 |
| 202 | minute_approximation | 49.782 秒 | 15.126 秒 | 近似层单列 |

全部 56 条观测的 `received_at` 均不晚于各自 target，并位于声明窗口；本地核对文件 hash、逐行输入 hash、八因子字段、拒绝原因与标签隔离均通过。没有证据证明这些人工样本的物理秒级取样精度，程序固定 90 秒也不等于已证实物理输送滞后。

### 封存与下一道门槛

全部 7 行 features/evidence/coverage、每条 sample/created 元数据、reading_id、received/source/target、质量、point definition、输入 hash，以及 182/197 理由已保存，未附带新灰分：

| 文件（均在 `output/gpt-expert81-holdout-20261102/`） | SHA-256 |
|---|---|
| `feature-evidence.json` | `e784f270921a788b5e0df03b581c0e292d4556ca2d2e83e13598bc85a3bc7757` |
| `feature-query-receipt.json` | `311a91d342c41ded4cd471e222e37e185bd9ca7a02298d41f048fea7f19211ca` |
| `feature-capture-manifest.json` | `402988f0f5171a0120dafbca4c7eae479efee4ca1b9b15e5b4660fc54be2a486` |

逐行 `input_sha256` 的规则是：移除该行 `input_sha256` 自身，按 JSON `ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',',':')` 编码 UTF-8 后 SHA-256。封存文件已经交研究代理，不再修改来追随其后代码变化。

研究代理使用已归档的 `source_snapshot` 核心，负责一次性生成并冻结全部候选的 selected prediction/coverage 及参照预测；不在这些新样本上重新选择候选或调参。本代理没有拟合或生成预测。只有主代理确认预测和代码 hash 已锁定并明确批准标签范围后，才可单独读取对应 ID 的灰分和元数据，保存首次完整标签 capture；禁止把标签与特征放在同一条 SELECT，也不调用 manual API。

## 第三阶段：锁定预测后的 6 标签首次完整捕获

研究代理完成一次性预测封存后，主代理独立核对 features、候选和预测锁，并对 6 条 C7 预测按冻结系数重算一致，随后明确批准本阶段。读取前已先保存 `label-reveal-plan.json`，规定主评、诊断、排除 ID、精确 SQL 和不可改锁规则。

被引用并在读取前后均保持原字节 hash 的锁为：

| 锁文件 | SHA-256 |
|---|---|
| `prediction-lock.json` | `bce7ff3e12010ecc27a0494829c6c9955aa1dc123d1868525528a844b7681e68` |
| `prediction-lock-manifest.json` | `c6f11685d24bf33197ce07544e4764fdee9d17aed5f466095e7ef23b0203ed0f` |

预测锁内部 hash 为 `da979aa824ef60b93f9bb3a63b04ac92872f6c2c54b7e9a648aa5ff1c33f12c0`。本代理没有修改原预测、候选、排序或输入资格。

授权范围严格为 `[185,187,190,195,200,202]`，只执行一次整数主键 ID 查询，`LIMIT 6`，耗时 **78 ms**，每请求超时仍为 3 秒，无重试：

```sql
SELECT id, value, observed_at, created_at, metric, time_basis
FROM manual_measurements
WHERE id IN (?,?,?,?,?,?)
ORDER BY id LIMIT 6;
```

该查询没有特征 JOIN，没有调用 manual HTTP API，也没有补读 182、191、197。6 行全部返回，`metric/observed_at/created_at/time_basis` 与锁定输入一致，缺失 0、元数据不一致 0。原值和元数据完整保存，未按预测误差、灰分大小或 190 的带煤量外推删除记录。

| 固定用途 | ID | 分母与边界 |
|---|---|---|
| 严格输入主评 | 190、200 | 两条均保留；190 已知带煤量外推仍留在分母，不按误差排除 |
| 分钟近似诊断 | 185、187、195、202 | 只单独报告，不并入严格层主成绩，不据此换模型、排序或资格 |
| 本次不取标签 | 182、191、197 | 分别为 seed 不可用、stopped 输入拒绝、用户排除；没有因本次授权扩大读取 |

首次完整值捕获为 **`2026-11-02T05:47:18.092+00:00`（北京时间 13:47:18.092）**，取自一次只读结果完整接收之后，`clock_domain=capture-process-clock`。没有宣称与主机时钟同步，没有把当前最终值回填为最初 `created_at` 已知，也没有把新标签用于改变本批已锁预测。今后只有在已证明、可比较时钟域内严格晚于可用时刻的预测，才可能合法使用这些标签作后续校准。

以下工件均在 `output/gpt-expert81-holdout-20261102/`，采用独占新建，不覆盖旧捕获：

| 文件 | SHA-256 |
|---|---|
| `labels-first-capture.json` | `61aedc6aa9a3de9fd8a011a0af880e870bc4a310c7289bf557df64e07a636b3b` |
| `label-query-receipt.json` | `a4f4f6209111e5ac9af4851e0868e9f5fa448947278d4d921964aa0df73f534b` |
| `label-capture-manifest.json` | `47a78df04e939a74a675a6f228081f205af7b56310450e8ab9147902f5b44183` |

原值位于 `labels-first-capture.json` 的 `raw_rows`，不在本审计文档抄列灰分数值。文件包含精确 ID、原值、原时间、元数据比对、锁引用和曝光台账。独立本地校验确认文件 hash、六 ID 范围、主评/诊断分层、锁原字节不变、无凭据、无额外标签或特征查询。

6 条标签现已向本任务揭示，已交研究代理仅据既有锁评分；以后不得再称作未见独立新样本。本代理不承担评分、不重训或调参，未触碰生产配置或原始记录。

## 第四阶段：扩大当前训练信息的生产补充与版本核对

用户随后授权扩大训练来源。主代理先明确仅补182，之后重新审视当前信息需要，另行明确批准核对 ID≤202、排除197的当前76条值。这是独立的新读取授权与新工件，不覆盖或替换原68冻结文件、原21条回放成绩、已锁6条预测/成绩；不读取新的 ID>202，留待后续验证协议处理。统一样本 inventory 由 `research_docs_max_sync` 单writer维护，本代理没有改 `coarse-ash-sample-inventory-20261102.md`。

### 当前77元数据计数快照

一次限定 metric 的索引查询在北京时间 **14:11:59.771** 完成，94 ms，返回77条元数据。北京时间10月16日零点以前的 observed/created 记录均为0，以后均77；排除用户已确认错误时间197后均76。12条记录的录入日期迟于采样日期。完整77行、18天 observed/created 日分布及来源限制已保存至：

`output/gpt-expert81-expanded-20261102/production_addendum/live-manual-coarse-metadata-20261102T141159.json`

SHA-256：`48293dfc15d8bfc5a12afa3a3334367b6f2f5d987e41ae1bf51a63691f25fe77`。这仍是元数据快照，不包含值；不能把77条灰分记录等同于77个完整八因子训练对，也不能据client_id证明与Excel不重叠。

### 182补充：新训练来源，不修复旧回放

182原 sample_at 为 `2026-10-31T00:19:05+00:00`，早于旧正式种子的 capture。其原锁定回放中的 `seed_not_available_at_sample` 覆盖拒绝保持不变；现在补取只能成为当前捕获之后可用的训练来源，不冒称原来就可预测或新增未见检验。

`id182-query-plan.json` 先落盘；使用已知schema证据，四条串行索引SQL分别取该时刻来源版本1行、八个已知点配置8行、八个窄窗观测8行，以及182灰分/元数据1行。液位只查 `[00:17:05,00:17:35] UTC`，即sample前90秒并向前30秒；其余七因子只查 `[00:17:05,00:19:05] UTC`。没有扩窗、额外lag或趋势窗口，也没有读191/197。

实际4条SQL合计218 ms，固定元数据GET为0；SQL耗时依次78、15、94、16 ms。八因子均严格有效，组为 `0,1,1,1,0,0`。182首次完整值捕获为 **`2026-11-02T06:34:06.561+00:00`，北京时间14:34:06.561**，元数据与此前记录一致，时钟域仍为capture-process-clock。原响应、输入证据、灰分原值、查询回执分别独立保存，`id182-capture-manifest.json` 的SHA为 `15dcf8436052916ba10d02674e2537b4c48dc7a9f563c687f053d03bc1ff1971`。

### 当前76值：一次精确有界读取

主代理随后批准新增一次当前值审计，不再单独重读182。读取前保存 `current76-query-plan.json`，复用既有 `ix_manual_measurement_metric_id`，每请求3秒超时、零重试，无分页、无特征JOIN、无manual HTTP API：

```sql
SELECT id, value, observed_at, created_at, metric, time_basis
FROM manual_measurements INDEXED BY ix_manual_measurement_metric_id
WHERE metric = ? AND id <= ? AND id <> ?
ORDER BY id LIMIT 201;
```

参数为 `coarse_slime_ash, 202, 197`。实际只执行一次，62 ms，返回预期76条，缺失0、范围内额外ID为0，197和ID>202没有读取。首次完整捕获为 **`2026-11-02T06:42:18.593+00:00`，北京时间14:42:18.593**。capture取完整只读结果接收后的进程UTC时间；observed_at保留原提交采样时间，created_at保留主机初次录入时钟，均不被当作修订时刻，也不宣称这些时钟已同步。

比较 exact numeric value 与元数据分别记录 same/different/missing，并保留旧、新文件引用和行hash：

| 旧证据 | 比较行数 | value same | value different | missing | metadata same |
|---|---:|---:|---:|---:|---:|
| 原冻结68 | 68 | 68 | 0 | 0 | 68 |
| 先前6条13:47捕获 | 6 | 6 | 0 | 0 | 6 |
| 182先前14:34捕获 | 1 | 1 | 0 | 0 | 1 |

这些结果只证明值在所比较的快照时点相同，不证明中间从未编辑。若本轮发现差异，新版本也只能从新capture起确证可知，不能按最初created_at倒灌；此次实际差异为0。原phase2 manifest、6条prediction lock及其manifest在读取前后均保持原字节hash。

191此次原值首次纳入原始清单，但既有 `coal_amount <= 10 / stopped` 资格拒绝不变，不用于训练。197仍为值未读取且用户明确排除。当前76值全部属于已看信息，不再作为未来未见独立样本。

### 交付的当前/历史双视图

以下均在 `output/gpt-expert81-expanded-20261102/production_addendum/`，没有改研究或inventory writer的manifest：

| 工件 | SHA-256 |
|---|---|
| `current76-values-snapshot.json` | `736f1ed5dcf9a6f4145eb3a1e67ac296e17c4953e40f9fad230fe82b75bbe268` |
| `current76-version-comparison.json` | `47e9fcada9716a58f5ca7bfd3fd6dc9a093a434dba5e8e839846f84d3e52409f` |
| `current76-capture-manifest.json` | `da7b6ae192b1f5024481a4cd446a34be921a32e603834858000053a192f8b645` |
| `db-current-source-rows.jsonl` | `adf465b46cccd11224320c15e4b3645845891aef6e9b96886cc507eae76ebe00` |
| `db-current-source-summary.json` | `ae2d49fdeb533efd98cbdd10914ea5d6a69b7554772d3718a5100666abb6708c` |
| `db-production-addendum-manifest.json` | `6bbf75076dd816c3da34243c67ba5b5c40545a543597a3e4f4b81fea5a7ff92c` |

对齐JSONL保留77个metadata ID，每行包括当前label值/版本/新capture、原old_snapshot_ref与historical_frozen_label、既有捕获与当前的比较、八因子及逐观测证据、group与质量。197的label为None。旧68历史开发视图仍引用原冻结值和原特征；当前76视图的 `label.available_at` 统一为14:42新capture，不能用旧created_at提前。两者不是静默覆盖关系。

全部77行的既有质量分层为 strict49、minute_approximation17、label_review7、incomplete3、user_excluded1。该分层不是最终模型训练数；strict/approx行仍须由已锁expanded协议决定组、来源和权重，191及197明确不训练，旧label-review/incomplete证据保留不填充。完整性校验确认所有文件/行hash、76值范围、197空值、191拒绝、182旧覆盖拒绝、原68历史字段、元数据对齐、无凭据和旧工件不变均通过。

上述当前快照、比较、182证据和77行对齐已交唯一inventory writer、研究代理及主代理。此生产读取分工不拟合模型、不改服务或参数，不会为趋势研究自行追加长窗口查询。
