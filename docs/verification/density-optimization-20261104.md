# 测试端密度候选优化与集成验证（DENSITY-OPT-1）

本次在笔记本只读测试端增加了独立候选建议及历史诊断，并修正“回算建议冒充当时先验”的比较口径。原小时参考、正式执行和候选分别展示；候选没有替换正式执行，没有部署生产或驱动控制。

测试端：**http://127.0.0.1:8000/**。最终实际监听进程 **PID 54584**，Windows venv启动器父PID 2268；`runtime-state.json` 和 `process-final.json`记录实际进程。实例保留供用户查看，技术启动及代理截图不等于用户业务验收。

## 实施范围与运行保护

- 后端由专属后端代理实施纯函数 `app.gpt_density.candidate.evaluate_candidate`、当前/历史GET接口及先验来源修正；前端由专属UI代理实施密度页展示；集成代理独占本地实例、端口和离线脚本。
- 生产默认不开放候选接口；测试实例显式使用既有 `remote-readonly.json` 并开启 `DMCS_DENSITY_EXPERIMENT=1`。专用runner在导入app前验证远端只读配置，绑定127.0.0.1，无reload、Hidden窗口。
- 本地HTTP运行模式实测 `readonly-test`，数据源是真实主机 `http://10.255.2.7:8000`。只读分支不启动采集、决策写入及保留清理后台，不执行建表/迁移；本地不存在的写探测路由POST被middleware **403** 阻止，真实趋势GET **200**。
- 未修改或覆盖原7天/月分析候选、57个原始月响应、13项月候选指纹及冻结模型/训练任务，没有生产重启、补档、回写历史或部署。

原 `backend/run.py` 固定8000且启用reload，未采用。初查8000/8010/8001/8765均空闲，选择8000；仅停止了核实命令行属于本任务的旧实例及启动器，未停止无关服务。Windows venv会生成实际Python子进程，因此以端口监听与runner记录的PID为准，不能将启动器PID当服务器PID。

## 候选行为与界限

候选参数为显式试验设置，没有拟合本月再声称盲测：

|项目|设置/行为|
|---|---|
|常规最大一步|0.010 g/cm³|
|暂估谨慎一步|0.005 g/cm³；另以 `provisional_mode='hold'` 做离线敏感性|
|灰分覆盖|严格>80%|
|当前设定/实测新鲜度|30秒；要求A304独立、同源同批同观测时刻及有效单位/定义|
|实测跟随容差|0.005 g/cm³；单次读数在容差内只称“跟随”，不称稳定|
|最近动作保护|灰分生产小时须完整位于动作观测区间之后才继续考虑追调；这不是识别物理最佳滞后|
|未知/过期/来源不足|明确不可用，不填0，不沿用旧候选假装新建议|

最后独立审查发现数学上恰好0.005的下侧差值会表示成0.0050000000000001155，造成上下边界不对称；仅跟随比较加入1e-7 PLC精度epsilon，并增加上下边界及明确超限用例，没有改试验名义容差或模型。最终 `candidate.py` SHA256为 `174ebc9e1d6fe540e015c54fc57bdf2746f66206138de9c5123d99be3a1f5c72`。

候选使用当前MD_SET决定下一小步，原小时参考仍保留当小时固定基准；正式执行仍单独显示其策略和原有±0.030限制。此次未实施此前已取消的“换小时基准”方案。

当前GET `GET /api/v1/decision/experimental-density` 返回原参考、当前设定、候选、观察阶段、质量与正式执行。`observation.actual_reading`明确实测值、时间、来源状态、新鲜度、与设定差及容差；无效值只作raw披露，不给有效value/difference。例如最终加载版本的固定探测样本：09:34:05观测实测1.513715、设定1.520，差−0.006285，`usable=true`且超出0.005跟随容差；这是该时间点证据，不代表页面之后每次刷新值不变。

历史GET `GET /api/v1/decision/evidence/density-candidates` 与趋势时间范围对应，给出快照条件试算及保持/等待/不可用原因；不把单次历史观测解释为专家确实见过或执行过候选。

## 先验比较修正的真实验证

从月冻结数据独立验证，旧数字匹配19条里有7条事后回算；修正后保守动作前有效默认实时档案为 **12条**。回算150条仅作离线背景，其87个有值小时不进入在线先验或候选效果评价。

原始月窗包含秒/毫秒，历史自定义API要求分钟精度，因此集成GET明确将起止各向下对齐14.517秒；**返回50动作ID集合与冻结月完全相同**，原始月文件未修改。最终实际趋势GET的保守比较状态为：26无在线档案、4跨小时不确定、12有效、8值缺失。字段在 `comparison.prior_availability`，并提供 `prior_hourly_advice`、`prior_difference`。

重点10-25 12:20那条回算1.558：最终线上测试响应中小时/先验建议和差值均不作为有效值显示，`ignored_replay_count=1`。这避免把事后计算写成“当时专家反向违背系统建议”。根线程也直接用冻结月raw调用已改比较函数，独立复算19→12及该案例。

## 冻结月上下文回放

脚本：`backend/scripts/evaluate_density_candidate_20261104.py`。仅读取冻结文件并调用纯函数，不开数据库或网络；每次核对57个原始响应及13项月候选SHA256。

必须分开两种资料：

1. **建议生成时已有完整读数的资料。** 241个默认实时revision中，仅2条能找到生成时已经计算完成、且读数年龄<=30秒的动作快照；其中1条参考有值，候选判断为等待实测跟随，另1条参考缺失不可用。90个有效最终小时里，仅1个有该即时上下文，其余89个明确不可比；239/241条没有运行候选函数，不能补成全月在线回测。
2. **变化前一条观测快照的条件试算。** 12条有保守在线先验。`evaluated_at`取 `before.decision_at`，通常晚于 `before.sampled_at`约5秒；真实动作可能已发生在两次观测之间，所以不能声称试算一定发生在真实动作之前。先验仍严格要求生成时刻<=保守sampled_at；latest_action只取当时已检测的前一记录，当前比较动作绝不预先放入输入。最终结算灰分没有喂给候选。

|12条条件快照试算|谨慎暂估模式|暂估保持敏感性|
|---|---:|---:|
|调整|4|0|
|等待|3|3|
|保持|3|7|
|不可用|2|2|

运行端点合格的10条分别为谨慎模式4调整/3等待/3保持，保持模式0调整/3等待/7保持。两条不可用位于停机端点且实测缺失。谨慎模式4个调整全部使用暂估依据，最大绝对一步0.005（其中一条只需0.004）；3个等待分别为1条实测尚未跟随、2条尚无完整调后生产小时。

输出保留原小时参考目标偏移、候选一步、快照即时建议、记录的人工变化及理由，不能把这几种量混为同一“原单步”。历史参数/送达版本不完整，未声称复现每个历史正式执行参数版本，更没有预测未执行候选会怎样改变灰分或产率。

双侧灰分描述继续引用冻结月事实：覆盖>80%且非暂估的90小时，低于8.5为42、8.5–9.0内35、高于9为13。它只是有记录小时的查询时结算分布，不是候选成效、全月生产达标率或产量占比。

## 技术检查和证据

集成输出根目录：`output/density-optimization-20261104/integration/`。

|检查|实际结果与证据|
|---|---|
|实例保护/真实GET|`probe-final-current.log`及`probe-final-month-verified.log`退出0；runtime readonly-test、首页标记、写探测403、真实趋势200、current200、历史50行200；当前含actual_reading。浮点单项修复后只补`probe-boundary-final-current.log`退出0/HTTP200，绑定最终源码hash与PID，没有重复月API宽测|
|最终离线回放|`replay-evaluation.log`退出0；原始hash、来源/先验性、当前动作排除、241/90完整性、12条件快照及步长断言通过；浮点单项后快速绑定最终纯函数，状态分布未变|
|月数据保护|57原始/13候选指纹一致；原月及7天目录只读复用|
|后端定向回归|最终candidate/API **52 passed**（含浮点边界）；来源/趋势 **33项**复用此前80项日志中未变化文件的对应部分；原小时基准/归档/回算/灰分建议 **59 passed**。三个唯一范围合计144项；没有把早期75/80/48批次重复相加|
|UI桌面/窄屏|11组DOM状态、9组原控件/口径检查、最终受影响渲染与390新首开/1440页头回归均退出0，errors=[]。初次390入口遮挡经授权仅窄修max720测试壳header后通过，未用force click/JS导航绕过。5个修改/新增文件加1个未改保护文件共6项UI指纹已固定|
|根线程独立核验|已独立检查真实current、冻结月19→12及回算案例；核对后端9项、UI6项来源指纹并亲看最终1440/390入口、依据、规则图。细节见 `output/density-optimization-20261104/root-acceptance-plan.md`；最终整体候选hash由根线程有限收尾核验|

中间失败保留：一次自定义API用原秒精度被422拒绝；一次集成脚本错误断言prior_availability在顶层而实际位于comparison，按真实schema修正后完整探测退出0。它们没有计作通过。此前后端超时批次也不能算通过；最终回归范围以具体最终日志为准。

后端日志位于 `output/density-optimization-20261104/backend/`：`candidate-float-boundary-tests.log` / `candidate-float-boundary-command.json`（52）、`candidate-final-tests-r2.log`（其中来源/趋势33）、`hourly-regression-final.log` / `hourly-regression-command.json`（59），对应命令退出码均0。UI命令、回归与最终6项指纹见 `ui/ui-delivery.json`、`ui/final-source-hashes.json`；最终画面为 `desktop-current.png`、`mobile-entry.png`、`mobile-current-details.png`、`mobile-rules.png`、`mobile-chart.png`。这些截图取自PID56272；最后只重载后端浮点单项至PID54584，未改变UI，未无故重拍。

离线复现命令（项目根）：

```powershell
backend/.venv/Scripts/python.exe backend/scripts/evaluate_density_candidate_20261104.py
backend/.venv/Scripts/python.exe backend/scripts/probe_density_candidate_test_20261104.py --label final-month-verified --candidate --frozen-month
```

runner入口为 `backend/scripts/run_density_candidate_test_20261104.py --port 8000 --enable-candidate`。当前实例已经运行，复现不需要再次启动或重启；只有端口负责人操作实例。

最终来源/输出指纹在 `output/density-optimization-20261104/integration/candidate-final-hashes.json` 固定；主动变化的服务stdout/stderr不作为静态候选hash，以免每次页面GET改变日志导致伪冲突。原分析57响应与13候选hash复核记录在回放输出内；用户业务验收尚未记录。
