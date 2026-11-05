# EXPERT8.1 笔记本影子运行与延迟化验对照

本轮实现独立本地 JSONL 研究档案和有界 CLI。冻结 EXPERT8/GPT v2 未改动，新模型未激活，未部署主机、未改变生产密度建议。正式选择与历史验证见 [模型研究报告](gpt-expert81-20261102.md)，原始时间和修订审计见 [数据可用性审计](gpt-expert81-data-availability-20261102.md)。本报告只描述运行、因果留档及合成边界验证，不把链路成功称为准确率提高。

## 时间与版本规则

- 所有笔记本 `recorded_at`、请求开始/收到时间都由运行时当前时钟产生；CLI 不接受历史 `--at`。主机与笔记本的 UTC 文本并不代表硬件时钟同步。
- 主机 `/live-cache` 的 `checked_at` 由主机每次请求现场产生。输入 `input_asof_at` 直接取该主机时间，原始输入 `received_at`、来源、definition、batch、质量、90 秒液位目标与实际时刻分别保存。
- 标签先在笔记本完整 capture 并 fsync，再请求主机时钟，追加 `label_availability_seal`。只有此后置 seal 才是 core 使用的保守主机域 `version_available_at`；不从 `created_at`、首次元数据读取或旧笔记本 capture 推测主机版本可用时间。
- 冻结种子也须在当前运行首次导入后取得新的主机 availability seal。10/31 审计的 capture-process-clock 不被重命名为主机时间。
- 预测只使用本地序列中此前已有、且 availability seal 严格早于 host fit cutoff 的标签版本。同一时刻没有顺序证据的标签不使用。
- 预测事件 fsync 返回后，再请求 `/live-cache` 追加 `prediction_seal`。这个较晚主机时间证明预测已先持久化，不能拿输入快照时间冒充预测已经产生的时间。

修订追加新 `label_version`，全局先解析 assay 最新可用版本，再按工况筛选。显式布尔 tombstone 停止之后使用，保留以前预测与评分。未知工况、无特征的最新修订也不能使旧版本复活。分页缺失不被解释为删除。ID 197 按用户决定始终隔离，数据库原值保留。

## 固定输入与配对条件

当前只支持已单独审计的三系统 `0,1,1,1,0,0`。二系统及其他组合输出 unavailable，不借用三系统参数。

七个非滞后输入要求 `0 <= input_asof - received_at <= 10s`。液位采用主机输入时刻前 90 秒目标、目标之前 30 秒内最新同 definition 原始读数。选最新点后判断质量，最新点坏时不绕过去取较旧好点；环缺失、租约/owner/definition 不匹配均缺失。另要求 `0 <= postseal - input_asof <= 10s`，独立约束网络与计算耗时。

正式样本配对固定为：同组，实际采样时刻严格晚于预测 postseal，且不晚于 `input_asof + 30s`；从合格事前记录中选择 `input_asof` 最新者，时间相同以原预测序号固定排序。选择不查看标签误差。缺少实际采样秒精度、工况或主机采样时钟来源确认，只留未配对原因，不给正式分。当前人工表本身没有实际取样工况和时钟确认字段，因此它的值和时间不足以自动认证正式样本。

普通标签不能携带任意 `audit_features` 冒充采样时特征。可训练的新标签只继承合法事前预测内保存的输入，并记录 `features_basis=issued_prediction_input` 及其 `features_asof`，绝不拿录入时液位拼旧样本。历史特征仅通过 `capture_frozen_seed` 导入：完整 artifact 哈希、种子 manifest 与 run header 必须一致，每行保留来源哈希。

评分始终引用已有 prediction hash，不调用 fit/predict 回算自身。修订另添新评分，旧评分不变。常规标签由当时预测训练集确定的 fence 分类，同时报告全样本；±1pp 使用 `1e-9` 数值容差。不按误差删除目标。payload 另外报告液位及煤量两项正常训练范围支持标记，煤量即使不是 C7 的拟合因子也不会隐藏超范围情况。

## C7 的事前 C5 残差证据

预测时同时冻结 C5 state/input/value/receipt。实际标签到达后，仅对固定窗口内的原预测追加 association proposal；proposal fsync 后再取主机时钟 seal。最终 association 保留原 C5 receipt 全部字节，不修改原 `sample_at`、不重新拟合、也不把新状态预测冒称旧预测。

registry 校验 `prediction < prediction_seal < label_version < proposal < association_seal < association` 的本地事件序，核原 receipt、label 具体版本、输入与训练特征哈希、group、clock domain/epoch、真实 host seal，以及原事前 latest-eligible 选择。core 无可信 registry callback 时拒收正式 association。消费要求 association `bound_at` 严格早于当前 fit cutoff。不同标签修订需要各自关联；旧关联不自动套到新版本。

初始正式种子的 C7 receipt 池为空。没有足够真正事前残差时清楚记录 `C5_zero_bias_insufficient_prequential_receipts`，不能把历史回放代理 receipt 混入正式池。

## 档案和故障恢复

run header 锁定 candidate/config/artifact/种子 manifest、core/runtime/CLI、冻结 EXPERT8 文件及推断代码哈希、主机来源、clock domain/epoch、配对规则。实现或 baseline 改动需新 run，不在旧运行悄悄替换。

本轮 candidate 的三种哈希含义明确区分：candidate 内部 `artifact_hash=8c5d2a54225d460e082dd7c8e7858bd19433ae87d7ccd2a8637d040b44fbbf71` 是去掉该字段后的内容 digest；运行 header 的 `artifact_hash=99aba01d22f84d04dc81627cbc85c48e79041cc1dedea18811fc204091565681` 是含内部哈希字段的完整对象 digest；`candidate.json` 文件字节 SHA256 为 `ee03b8ea7a701a498715cc9b413ac03136fe1412a0207229ad7d39415a208e64`。这三者没有互相替代或混称文件 SHA。

JSONL 以操作系统文件锁串行验证与追加，完整行带序号、前一行哈希、存储行哈希和展开后 payload 哈希。训练 records 按内容哈希单独写一次 `calibration_blob`，state 与原 receipt 存引用；读取时恢复精确原对象，再验证 core state/receipt 哈希。不保存整段 live ring、不另建业务数据库、不自动删除。

有效前缀读取后也 fsync，防止上次写全行但 fsync 失败后，幂等重试直接把尚未确证持久化的数据拿去盖 host seal。部分行或哈希损坏保留原字节并拒写。label seal 后 proposal 尚未写完、或 association seal 后最终 envelope 尚未写完，都可从原持久化 seal 完成终态，不重新选择预测或拟合。已完成操作的幂等重试不消费过期 anchor。

新请求在前一个响应之后开始且主机时间倒退时，记录 clock fault 并停止该 epoch。正常重叠请求/在途旧 frame 只拒绝并要求重新取数，不误报主机时钟倒退。不会自动修改 Windows 时钟。

## 轻量只读接入与命令

`ReadOnlyCacheSource` 每次读取 live-cache，再用只读传输读取三张小表的精确列：最新 `decision_source_revisions`（LIMIT 1）、所需 enabled `telemetry_points`（LIMIT 65，超 64 拒绝）、对应 `telemetry_sources`（LIMIT 9，最多 8 来源）。没有遥测大历史查询，不调用 `/decision/current`、hourly、density、manual-measurements 或训练入口。参数化 WHERE 只覆盖八因子默认或显式配置的来源。

CLI `tick` 默认 1 次、最多 3 次、间隔至少 30 秒；本轮仅运行一次，不长驻。`watch-files` 最多 120 次且标记 file_exploratory，文件里伪造 transport/时间字段不能成为正式取数证据。标签由明确提供的本地文件导入，不自动读取新化验列表。

在项目根 PowerShell 运行（下一次启动是否采集，按用户继续授权的范围执行）：

```powershell
& .\backend\.venv\Scripts\python.exe .\backend\scripts\expert81_shadow.py init --ledger .\output\new-shadow-run.jsonl --artifact .\output\gpt-expert81-20261102\candidate.json --clock-domain production-host:10.255.2.7
& .\backend\.venv\Scripts\python.exe .\backend\scripts\expert81_shadow.py ingest-labels --ledger .\output\new-shadow-run.jsonl --labels .\output\gpt-expert81-20261102\candidate.json --frozen-seed
& .\backend\.venv\Scripts\python.exe .\backend\scripts\expert81_shadow.py tick --ledger .\output\new-shadow-run.jsonl --count 1
& .\backend\.venv\Scripts\python.exe .\backend\scripts\expert81_shadow.py summary --ledger .\output\new-shadow-run.jsonl
```

默认只加载现有 `backend/data/remote-readonly.json`；也可显式 `--remote-config` 指定既有只读配置，凭证不写档案。原始本地标签文件用 `items` 或数组，每项含 assay_id/value/sample_at/group_key/sample_precision/sample_clock_domain/sample_clock_evidence；删改是显式单 ID 新事件。正式 clock evidence 必须来自实际取样登记，不能用录入时当前工况代替。

## 本轮现场与测试证据

先完成一次输入提取：`output/gpt-expert81-20261102/shadow-input-inspection.json`。现场 011100，七非 lag age 均 0.391s，level age 90.658s，未生成预测、未读新标签。

再按已锁 C7/34 旧种子完成一条真实事前预测：

| 项目 | 实际记录 |
| --- | --- |
| run | `a3604efd-1657-40bb-bba4-88f88674ba15` |
| seed host availability seal | `2026-11-02T05:47:55.919000+00:00` |
| host input / fit cutoff | `2026-11-02T05:47:56.903000+00:00` |
| notebook prediction recorded | `2026-11-02T05:48:37.111057+00:00` |
| host postseal | `2026-11-02T05:47:58.138000+00:00` |
| postseal snapshot age | 1.235s |
| 七非 lag / level age | 4.890s / 90.109s |
| 训练 / 常规 / 正式残差数 | 34 / 32 / 0 |
| C7 / frozen EXPERT8 | 12.22658208961277 / 18.69319545068737 |
| fallback | C5 zero bias，正式事前残差不足 |
| prediction hash | `94382d98c5437fb2e0f693a58ee0f31296a6051cc462822186dbe2a1321aafe9` |

实际 ledger：`output/gpt-expert81-20261102/shadow-live-c7-20261102.jsonl`。完整哈希、双时钟、来源/新鲜度、strict seed-before-cutoff 检查在 `shadow-live-audit.json`；CLI 执行输出在 `shadow-live-cli-verification.json`。这条记录没有后续正式化验评分，MAE/±1pp 样本数为 0。主机与笔记本仍有约 39 秒时钟差，实际取样 clock 认证仍须现场补足，未做任何伪校时。

原样重复训练 records 的原型测得单预测 99,667 bytes；内容去重后实测 prediction 行 28,609 bytes，postseal 904 bytes，一次 calibration blob 24,858 bytes，含 34 种子与首预测的整个 ledger 106,133 bytes。若以后保持同一训练记录集合，按当前行规模推算 30 秒持续留档约 81 MiB/天，60 秒约 41 MiB/天，尚未启用这种持续运行。未来长驻前仍应约定合适节奏及进一步状态去重，不能把本轮有界试跑当成长期容量验收。

运行时合成边界 39 项通过，覆盖自身泄漏、同秒、38 秒双钟差、迟到/修订/未知组/删除、C7 immutable association/registry、缺 group/精度/clock、窗口/新鲜度、幂等/重启/并发、在途乱序、两处中途故障恢复、一次 fsync 失败、截断/哈希变更、伪造文件/audit、baseline pin、内容去重、煤量支持标记。与 core 及旧 EXPERT8 live 联合回归共 99 项通过；只有既有 Starlette/httpx 弃用提示。所有测试使用临时文件和合成标签，未读写生产库。
