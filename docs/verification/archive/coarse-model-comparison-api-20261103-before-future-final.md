# 粗精煤泥双模型比较只读 API — 2026-11-03

护栏后最新绑定（数值不变、尚未最终seal）：受控汇总及实际GET已重导，model SHA256改为 `b768cb2a8b2b8c37b67f76030ad0aeb535afb69d5b71230c4fe2d3facac5a2e0`；artifact SHA256=`b63c903687c23ffa5dc4bc06d50db3a64be5fbb016a6b7f2af8db6bd727247ed`。freeze显式记录freeze-manifest与sealed-runtime-manifest哈希，provisional及sealed_at/due_at=null。新四反馈组合hard guard与训练说明已绑定：当前EX8原177旧样本、经验液位基线+7因素±2有限修正；候选实际36条strict常规DB，动态液位/煤量GBR，训练反馈恒A0/B1/4011/4021，不能宣称学会开关变化，新mask拒绝。候选full实际训练10-16~11-03；EX8原表日期歧义，当前training_date_range明确null，绝不套用候选日期。history76、forward64、全7指标不变，futurepending/null。只重导与HTTP字段核验，没有重训练、激活、部署或改模型。

最新真实交付（替代下方旧GB研究快照）：生产优先候选已从新production-freeze证据导出，version=`EX81-PRODUCTION-SELECT-20261103-v1`，model SHA256=`af7274fed3070f870df289ca05f81f2e5f8a3bccb708b3cc43611223458f19a8`。候选为三系统严格标签训练的绝对误差损失梯度提升回归（80树、depth2、leaf5、lr0.05），二系统保留实际14/15。HTTP200、research/provisional；尚无实际seal时间，future保持pending/null。当前受控artifact SHA256=`8b9b0d637e16e7eb0691dbcb842240f6e7f4de058c2fec6660d0818f303e7956`。真实140行CSV（历史76+forward64）已独立复算所有误差，并与API response一致，16测试通过（0.73s）。真实响应位于 `output/coarse-model-comparison-ui-20261103/api-response.json`，可重复运行同目录 `verify-api-response.py`，只挂独立router并使用隔离临时SQLite配置。

默认三系统真实七指标：Historical Evaluation（58/60，2026-10-16~11-03）候选MAE1.628344、RMSE3.296575、Bias-0.808503、R²0.013312、±1命中34/58=58.62%，coverage96.67%；EX8同58条MAE3.690173、RMSE5.328908、Bias+1.678062、R²-1.578282、命中10/58=17.24%。TSCV（48/54，实际评估10-18~11-03；候选折训练10-16~11-02、16fold）候选MAE1.980998、RMSE3.603841、Bias-0.894568、R²-0.009654、命中20/48=41.67%、coverage88.89%；EX8同48条MAE3.035035、RMSE4.595643、Bias+0.866010、R²-0.641851、命中10/48=20.83%。负R²保留，审查组actual常量时R²=null。全部三系统forward包含3个突出值，normal45仅补充；normal的全目标分母未知，coverage=null。全组合forward64/70，其中二系统16/16只固定规则历史回算，没有学习训练，不伪造日期。

本轮新增指标与生产优先选型：所有Score（overall/by_system/normal/audited/review）已包含MAE、RMSE、Bias（prediction-actual）、真实R²、±1pp命中率、N、eligible_n、coverage及实际配对日期范围。RMSE/Bias/R²与MAE完全同分母；R²可负，n<2或actual无方差时null，不用force_finite。metric/system附候选实际train_date_range、fold_count/fold_description/training_note；冻结现用EX8不继承候选折训练日期。覆盖率使用全部阶段目标含预测拒绝，未知normal目标分母保持null。16项针对性测试通过（0.65s），包括负R²=-25、单条/常量R²null、Bias正负、RMSE同池、日期与未来全null、真实系统及review重算。

用户最新优先生产预测，旧GB400仅保留provisional研究状态，下面旧GB数值是先前实证记录，不是最终生产候选。主线程已确认新的三系统选型为S__BOOST_absolute_error_2、二系统保留固定14/15；待训练者production-freeze新evidence导出后在本文记录实际GET。新组合forward应按真实64/70（3系48/54+2系固定规则16/16）核对。CSV新增training_mode=fixed_policy的唯一零训练例外：仅2系且双预测相同并为14/15，训练日期必须空；fold_count0、training_note明确固定规则历史回算。三系统learned fold仍严格train_max_date<target date，不能伪造训练日期。候选未获实际最终seal前保持research/provisional。

实现 `GET /api/v1/training/coarse-model-comparison`。该 GET 不获取数据库 Session，不运行训练、不反序列化 joblib、不启动采集、不改模型激活；仅读固定打包文件 `backend/data/gpt_coarse_model_comparison.json`。现有 `/training/coarse-model` 等接口兼容。

API 服务 `backend/app/services/coarse_model_comparison.py` 使用严格 schema 与字段白名单（extra=forbid），限制文件大小并验证完整 artifact SHA256。文件缺失返回 missing；坏格式/坏 hash 返回 invalid；显式有效期过期返回 stale。以上均无指标数值，不伪造成 0。未来无结果为 pending，MAE/hit1/rate=null、N=0。HTTP 不接受文件路径；API 不公开逐行原始化验、record ID、模型文件或本地证据路径。

`backend/scripts/export_coarse_model_comparison.py` 是显式离线导出器。输入目录有 metadata.json、predictions.csv 和两个输入文件的 SHA256 manifest。导出器逐行重算两模型 overall、二/三系统及 normal 的 MAE/±1pp 命中；历史 coverage 用全量 scope.total_n 作分母。相同 CSV 行强制包含两模型的有限预测及实际标签；重复 identity、缺单边预测、scope 缩小、future 没有 accepted seal 均拒绝。Forward 要有 fold 与严格早于目标日期的 train_max_date，不能把同日更早化验当可用。方法描述来自受审计 metadata，导出本身不选择赢家、不推断 final-freeze 即已封存。

扩展口径：scope 为全部81、合法可回算76、已审计训练资格70、仅审查诊断6、拒绝5。11个不具训练资格不等于11个不可预测。scope.by_system 为2系18/18/16/2/0，3系60/58/54/4/2，unknown3/0/0/0/3（顺序 total/evaluable/audited/review/rejected）。服务无这些固定人数，均从实证metadata加载并严格校验合计。history_all overall和by_system均单列audited/review子指标；normal为训练fence正常性，独立于资格分类。method.by_system逐系统解释旧规则/模型，2系明确实际14/15生产规则。

导出命令（使用项目 Python 环境亦可）：

```powershell
backend/.venv/Scripts/python.exe backend/scripts/export_coarse_model_comparison.py --evidence-dir output/gpt-expert81-db-primary-20261103/comparison-evidence
```

原子替换唯一页面汇总文件，另在证据目录写 `metrics-recomputed.json` 供复核。无 DB、网络、训练或部署调用。逐行 evidence 留在本地用于独立复算，不通过 GET 传给浏览器。冻结时间、最终赢家和未来评分由主线程审核训练者实证后提供，导出器绝不把首轮赢家写死。

完整契约：`output/coarse-model-comparison-ui-20261103/api-contract.md`。UI 三张指标：Historical Evaluation · MAE、TSCV · MAE、OOT · MAE；单位 pp。默认读取三系统 `metrics.<phase>.by_system["3"]`，二系统独立；overall 不替代三系统主比较。

针对性验证：`backend/tests/test_coarse_model_comparison.py`，14 passed（0.68s）；完整日志 `output/coarse-model-comparison-ui-20261103/api-tests.log`。覆盖独立数值预期、同池/分系统/normal、audited/review划分与错误分类拒绝、future pending、hash mismatch、同日训练拒绝、单边缺预测、重复目标、未来无seal、隐藏全量scope、只读加载、白名单、过期、显式导出范围；新增真实127行CSV逐整体/分系统/资格组使用独立math.fsum复算MAE和命中，与实际HTTP响应一致。FastAPI TestClient 仅挂独立 router，无应用 lifespan/worker；pytest 使用仓库 conftest 的隔离临时数据库，未连接生产库。

真实证据已导出并HTTP200核验：`backend/data/gpt_coarse_model_comparison.json`。响应另存 `output/coarse-model-comparison-ui-20261103/api-response.json`。当前status=research，freeze=provisional（真实seal时间尚未生成），history_all76、forward51、future pending/null。当前 artifact SHA256 `a4ed1e383e6cd292a12270785f72e91acfe76eed8387f5da8f5c548463897018`。独立核算总体历史候选MAE0.527451、旧实际策略3.454474；默认三系统58/60历史候选0.640649、EX8 3.690173；forward三系统48个配对候选2.579440、EX8 3.035035。以上历史开发诊断，不是独立未来效果。GET前后packaged文件字节完全不变；响应核验环境使用临时SQLite文件路径，未启动应用完整lifespan。初次尝试内存SQLite因仓库元数据codec要求显式catalog被拒，改隔离临时文件后通过，未访问生产DB。

限制：manifest 是文件完整性检查而非外部签名；freeze accepted 由主线程独立验收实证提供。页面打包内容不自动追踪外部生产 DB；最终seal后以及新未来评分必须显式重新导出并更新本文响应证据；本轮不部署。当前历史数值以页面受控 JSON 及对应 evidence 审计为准。
