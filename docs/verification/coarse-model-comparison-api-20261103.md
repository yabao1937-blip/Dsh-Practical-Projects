# 粗精煤泥比较 API 当前交付状态 — 2026-11-03

## 当前实证与状态

候选版本 `EX81-PRODUCTION-SELECT-20261103-v1` 已独立验收封存，API status=ready、freeze.status=candidate.status=accepted。实际封存时间2026-11-03T07:44:40.311965Z，到期2026-11-10T07:44:40.311965Z（北京时间两日均15:44:40），严格168小时。候选model SHA256=`b768cb2a8b2b8c37b67f76030ad0aeb535afb69d5b71230c4fe2d3facac5a2e0`；runtime manifest SHA256=`602dd123b7bc274cebfdea8d9923b06287e7d875a8875670f0296b955e7d3975`；final seal SHA256=`879ec180143034e6f0128a62969aa3160c0da8bce975f9d173b7fd4c13d032d2`。包含未见四反馈组合拒绝护栏。封存不代表激活或部署，也不代表已有未来成绩。页面受控文件为 `backend/data/gpt_coarse_model_comparison.json`，实际HTTP响应为 `output/coarse-model-comparison-ui-20261103/api-response.json`；新实证必须显式重新导出。

真实证据CSV共140行：history_all76，history_forward64，future0。数据库scope全部81、合法预测76、训练资格audited70、仅审查诊断review6、拒绝5；11个不具训练资格不等于11个不能预测。系统拆分顺序total/evaluable/audited/review/rejected：三系统60/58/54/4/2，二系统18/18/16/2/0，unknown3/0/0/0/3。服务不硬编码人数，完全由metadata提供并验证合计。

三系统现用EX8是原177旧样本拟合，经验液位基线加其余7因素±2有限修正，参数冻结；旧表日期存在歧义，所以当前训练日期明确null，不套候选日期。候选实际36条strict常规DB训练，液位/煤量动态绝对误差损失梯度提升回归：80树、depth2、leaf5、lr0.05。四反馈训练时恒A0/B1/4011/4021，仅界定已覆盖工况，不宣称学会开关变化；未见组合拒绝。候选full实际训练2026-10-16~11-03。二系统候选保留实际14/15规则，零训练。

## 当前七指标

默认三系统历史合法回算58/60，实际评估2026-10-16~11-03：

| 模型 | MAE pp | RMSE pp | Bias pp | R² | ±1pp | N | 可评分覆盖 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 现用EX8 | 3.690173 | 5.328908 | +1.678062 | -1.578282 | 10/58 (17.24%) | 58 | 58/60 (96.67%) |
| 候选 | 1.628344 | 3.296575 | -0.808503 | 0.013312 | 34/58 (58.62%) | 58 | 58/60 (96.67%) |

默认三系统TSCV48/54，实际评估2026-10-18~11-03，候选折训练2026-10-16~11-02、16fold，严格只用更早日期：

| 模型 | MAE pp | RMSE pp | Bias pp | R² | ±1pp | N | 可评分覆盖 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 冻结EX8同池 | 3.035035 | 4.595643 | +0.866010 | -0.641851 | 10/48 (20.83%) | 48 | 48/54 (88.89%) |
| 候选 | 1.980998 | 3.603841 | -0.894568 | -0.009654 | 20/48 (41.67%) | 48 | 48/54 (88.89%) |

全组合TSCV64/70=三系统学习模型48/54加二系统固定规则16/16。二系统是规则历史回算，没有学习训练，不伪造train日期；未来阶段则明确称固定规则纯推理。以上历史与TSCV均是开发诊断，不是独立未来效果。normal仅补充；三系统forward normal45，完整normal目标分母未知，coverage=null。audited/review分别独立报告；review actual常量时R²=null。

## Future 独立可用性与公平交集

当前尚无真实future行，双方及future_comparison均pending，MAE/RMSE/Bias/R²/命中率/coverage/date_range全null，N0。未来明确冻结参数纯推理，不重训、不调参，fold_count0；实际metadata与GET已绑定现用baseline_id=EX8_PRODUCTION_RUNS，label=现用EX8生产RUNS口径（参数冻结），现用EX8参数不变、实际生产RUNS输入；候选baseline_id=EX81_PRODUCTION_CANDIDATE。历史两阶段保留原native legacy口径，不回填RUNS，服务不耦合传感语义。

仅future允许current_prediction/candidate_prediction各自为空，但actual必须finite，缺预测一侧必须有安全reason code。每模型独立计算有效预测+已揭标签的N及全部误差；一侧无法预测应status=unavailable、N0、误差null、coverage0，另一侧有有效预测仍报告真实成绩。历史两阶段继续强制同池。

Future系统范围兼容0/1/2/3/4/unknown并独立保留by_system；生产现用仅count2走14/15，其余0/1/3/4在8输入合法时可EX8推理。候选仍限2/3，因此0/1/4可能合法old-only；这些记录保留在current overall，不能因为candidate拒绝就删掉或当作current不可用。unknown必须双方不可预测。历史CSV仍严格2/3，不改变140行实证。

`coverage=n/全部已揭标签目标（含拒绝）`是可评分覆盖率。`labelled_n`、`pending_label_n`、`attempted_n=labelled+pending`明示未出标签目标。跨全部尝试的`prediction_coverage=prediction_n/attempted_n`只有完整预测有效数已知时才给，否则null，不能用已评分N假装全预测覆盖。预测计数目前仅global metric，系统视图须注明“全部系统”或仅用本系统可评分覆盖。

顶层`future_comparison`（及by_system）只取双方均有预测与标签的交集，含common N/coverage及该交集current/candidate全部七指标。两模型各自都有分数但交集N0时，common unavailable、误差null，不能声称配对进步。无标签时common pending。baseline_id/label位于current.metrics.future及其系统记录、future_comparison；未来有已揭标签行必须有明确baseline身份。

## 实现、复现与核验

`GET /api/v1/training/coarse-model-comparison`只读固定JSON，无DB Session、训练、joblib反序列化、激活、网络或写入。服务使用严格字段白名单、大小上限与规范JSON SHA256；缺文件missing、坏schema/hash invalid、显式过期stale，均不伪造指标。HTTP不接受任意路径；不公开逐行标签、ID、模型文件或本地证据路径。

```powershell
backend/.venv/Scripts/python.exe backend/scripts/export_coarse_model_comparison.py --evidence-dir output/gpt-expert81-db-primary-20261103/comparison-evidence
backend/.venv/Scripts/python.exe output/coarse-model-comparison-ui-20261103/verify-api-response.py
```

导出器验证metadata/predictions文件hash，独立重算overall/system/normal/audited/review MAE/RMSE/Bias/R²/命中，atomic替换固定页面JSON并写证据目录metrics-recomputed.json。所有误差同有效行分母；Bias=prediction-actual均值；R²真实1-SSE/SST，负值保留，n<2/实际无方差null。Forward learned行严格train_max_date<targetdate；仅明确2系fixed_policy且双预测原14/15规则才允许零训练日期。

定向测试：`backend/tests/test_coarse_model_comparison.py`，27项通过（1.23s），身份字段后补7项定向通过（0.96s）。包括负R²、常量/单条null、Bias符号、RMSE同池、真实140行全层级独立math.fsum复核、左缺/右缺/都缺、双方各自available但交集N0、pending标签、拒绝原因、old-only0/1/4保留而历史4系拒绝、future纯推理文案、只读与坏hash、冻结当前不套候选日期。日志为 `output/coarse-model-comparison-ui-20261103/api-tests.log` / `api-future-identity-tests.log`；不存在真实未来评分。正式seal后仅重新导出与必要字段HTTP核验，没有重复全套：`api-final-seal-verification.json`确认HTTP200、accepted、两时间相隔168h、model/runtime/seal各hash准确，4块历史整体/分系统/附属七指标对象逐字不变，future双方与common均pending。HTTP核验只挂独立router，用隔离临时SQLite配置，不启动完整应用lifespan/worker；GET前后受控JSON字节不变。最终受控artifact SHA256=`c56e74f819b9ff1f689fbab47463019c49db314168a236bcfd070e92679d8c9e`。

完整契约：`output/coarse-model-comparison-ui-20261103/api-contract.md`。Manifest用于完整性检查而非外部签名；本次accepted来自主代理独立核验真实seal。新future证据显式重新导出。本轮无训练、模型修改、激活、采集、主机部署或生产DB访问。

## 历史报告归档

旧GB研究、旧manifest/127行/forward51等检查过程已移到 `docs/verification/archive/coarse-model-comparison-api-20261103-before-future-final.md`。该档案不是当前状态，不得将其中旧赢家、分母、hash或“最新”措辞当作当前验收结论。
