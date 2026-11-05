# EXPERT8.1 本地研究来源语义防护（2026-11-03）

用户确认 RUNS 正常，并授权活跃脱粉点位从旧 `if_prod` 切换为 RUNS。本轮另行修正笔记本研究用 shadow 的来源校验，避免 RUNS 恰为 False/False、数值仍为 `0,1,1,1,0,0` 时，误用旧 if_prod 域的 C7 种子或校准。该研究代码未部署，未读取 ID204/206 灰分，未进行研究数据拟合或启动连续收集。

## 改动与限制

- 新 run spec 使用 v2，显式保存旧 if_prod 观测域及完整 artifact/seed 哈希证据。旧域仅描述来源，不证明物理停机。冻结 C7 缺少显式来源字段，故只对已经审计的完整内容 digest `99aba01d22f84d04dc81627cbc85c48e79041cc1dedea18811fc204091565681` 和 seed digest `1cd008a3bfe6e1b79ae2d4e299391c55d246e14aeb265562bed63283471e3db8` 提供精确兼容绑定；未知 artifact 自填旧域也不获得绑定。这些 canonical 内容 digest 与原文件字节 SHA256 是不同口径。
- cache adapter 从实际 point config 记录 item/node 定义、definition_id 和 raw sample；RUNS 观测即便数值组与旧主组相同，也标为不适用，不生成冻结基线预测。直接调用 predict 时再次验证两点来源、重算定义、raw sample 自身 node_id/定义，以及 raw Bool/0/1 与 cell 规范二元值一致性，校验失败在任何 fit/receipt 之前抛出明确拒绝，不制造零 bias 回退。
- 标签版本保留来源语义。评分/关联要求标签、原持久预测和 run 同域；最新缺域、异域或不可用版本会抑制旧版本，不回退继续校准。冻结种子只能来自已固定 artifact，内部审计证据绑定同一来源域。有效历史的来源声明进入 training provenance 与状态哈希。
- 原始 E5 文件当前不符合这个 C7 shadow 的 artifact 接口，本轮没有将 E5 接入，也没有授权跨语义复用它的当前 35 条校准标签。未来 RUNS 候选需要独立来源合同与审计，不能把新 artifact 加个域名就当成已获支持。
- runtime 内容哈希已变化；旧真实 ledger 保持原字节，只做独立静态核查，不使用新 runtime 续写。生产 payload、冻结模型参数及旧报告没有在本子任务中改写。

修改文件为 `backend/app/gpt_coarse/expert81_shadow.py` 与 `backend/tests/test_expert81_shadow.py`。实现前研究文件 SHA256 为 `fa8b385279c0ea7832c258db575cf7d42b1fb8b56ebcbc1ad9a116184c65a335`；本轮最终实现为 `ecd71193771a87376edf89b676901505077a4ee88ab2a592a6e1662a0f6bc270`。

## 验证

`backend/.venv/Scripts/python.exe -m pytest tests/test_expert81_shadow.py -q`：最终 **66 passed in 4.31s**。数据均为合成数据、journal 均为临时文件；真实冻结 C7 只读检查 spec 绑定与字节哈希，不据此拟合。第一轮 61 项通过后，独立 reviewer 指出应同时核对 raw sample 自身 node_id 与取值；已补实现及 5 个反例，并完成最终重跑。

定向覆盖 RUNS False/False 与旧主组数值相同仍拒绝、直接 frame 只伪装显示字段仍拒绝、raw node 为 RUNS 或 raw/cell 二元值不一致时拒绝、缺失/格式错误来源元数据在 fit 前拒绝、旧 if_prod 合法路径有效、来源不明 artifact 不自动认定旧域、最新异域/缺域标签抑制旧校准版本、缺少种子来源证据不进 history，以及旧 runtime ledger 拒绝续写且字节不变。原延迟化验、版本/删除、时钟、事前 receipt 关联与幂等测试也通过。

最终结构化验收见 [validation-final.json](../../output/gpt-expert81-resume-20261103/source-semantics-guard/validation-final.json)。首轮 `validation.json` 保留为独立复核前的历史记录，其源码/测试哈希与 61 项计数不代表最终版本。最终源码 `ecd71193…` 已由独立 reviewer 只读复核通过，并另做纯合成 probe：旧 if_prod 的 Bool→0 有效路径接受，raw 节点伪装为 RUNS、raw/cell 值冲突、缺 raw 节点及 RUNS 声明域均拒绝。`validation-final.json` 中的 pending 是该文件写入当时的状态，现已由此独立结论闭合；本次输入审计进展只补记新报告和草案，不改该历史验收文件。

C7/E5 两份冻结 manifest 所列 **12 个输出文件哈希均通过**。原真实 ledger 独立静态检查为 39 个事件、1 条预测，hash chain 无错误，预测事件哈希与旧 `shadow-live-audit.json` 一致；文件为 106133 bytes，SHA256 `ab8233340cac81a396f9c63fed9aa34390c76f743e2e72dd2c2afcfa3f2c53d4`。

## 后续小池草案

见 [RUNS 恢复协议草案](gpt-expert81-runs-resume-20261103.protocol.draft.json)。P0/P1/P2 仅为待 root 锁定建议，没有执行或选型。拟用同一真实三系统 RUNS 组合的中位数、液位稳健回归、合法事前残差收缩三项比较；二系统保留独立分支，来源混域及未来标签泄漏均不得用回退掩盖。

随后按 root 单独批准完成 ID204/206 的无标签输入清点：**17 SQL、0 catalog GET、0.375 秒**，未查手工表或读取目标灰分。source 均为 `kepserver-main`，脱粉明确使用 RUNS；四系统按原固定点位读取。实际组合如下：

| ID | 采样时间（北京时间） | A/B/401/402/RUNS473/RUNS474 | 四系统反馈数 | 研究分支 | 历史输入配对 |
|---|---|---|---|---|---|
|204|11-02 21:26:18|`0,1,1,0,1,0`|2（B、401）|二系统，独立组合|t−60/90/120 均 strict|
|206|11-03 00:03:17|`0,1,1,1,1,0`|3（B、401、402）|三系统，独立组合|t−60/90/120 均 strict|

沿用既有**历史**规则：非液位 age≤30 秒为 good、30<age≤65 秒只作分钟近似；液位只取各 t−lag 目标之前30秒内的最新记录。两条固定七因子分别接收于 `21:26:00.861` 和 `00:03:04.298`，年龄17.139/12.702秒，各自均同source/batch/received_at；三种液位目标的年龄分别为17.361/17.408/17.564秒与12.882/12.936/12.889秒，原始类型、质量及身份检查全部通过。因此六个输入配对均满足历史 strict，不因超过10秒而降级。**在线非 lag 的10秒 receipt 门槛仅用于在线路径，继续保留，不套给历史配对。** 三个lag的输入资格通过不证明物理最优lag，也不会把2个样本扩算为6个独立标签。

两组**各仅1条输入证据**，不能合并成同组两条或跨两日期训练支持。ID204不并入三系统P0/P1/P2，也不借旧 if_prod 二系统关系凑支持；ID206的新RUNS组同样不能继承旧C7种子或E5旧域的35条校准标签。原始采样精度和采样钟对齐仍未知，灰分继续未读，常规标签成员资格及完整版本可用时间尚未确定；二者均没有采样前保存的新RUNS池receipt，暂为各自待审核的桥接/未来seed候选，不评分、不训练或称为事前验证。还需每个确切来源域/组合自己的可信样本、日期与变化范围证据。草案中的5个常规标签、2个日期只是待root确认的本轮运行门槛建议，不是普适可训练定律或80%准确性证明。

无标签输入证据见 [input-only/README.md](../../output/gpt-expert81-resume-20261103/input-only/README.md) 和 [input-result.json](../../output/gpt-expert81-resume-20261103/input-only/input-result.json)。结果SHA256为 `23c21c327927fd88af146053497f1ebdae2229c72e7c555862625ba60a3e5d03`，最终manifest SHA256为 `8438158d6176f80e98b96cf3e61f15c4efcedf9e8954200094015950bbfe1940`。revision 9只记录复合desliming选择，不能据此反称当时生产已切RUNS；本清点是新来源域下的并行历史输入证据，不回写旧冻结配对。`created_at`仍只表示初录，不能当报告/修订或当时标签已可用的时刻。

本次补记仅更新这份新报告和新草案的输入证据、分组及复核状态，未改实现、测试、旧冻结文件或任何目标标签。历史验收文件中的报告/草案SHA对应其写入时版本，当前文档哈希随此次进展补记更新，不冒充旧校验中的字节版本。
