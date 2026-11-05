# 2026-11-03 密度趋势三图与脱粉 RUNS 主机部署交接

主机部署验收记录时间：2026-11-03 13:24（北京时间）。状态：**主机部署完成，准确整体修订 installed/healthy=true、用户 UPDATE INSTALLED 确认及轻量装后验收均已取得。** 该时间仅指主机部署验收；后续EXPERT8.1研究状态见下文用户恢复后的记录。

## 范围与当前入口

主机为 `http://10.255.2.7:8000`，安装根 `C:/Users/XMC/DshDensityControl`，数据库与备份经 junction 位于 `D:/DshDensityControlData`。笔记本门户为 `http://10.255.2.6:8765/`。本轮部署只交付三个趋势前端文件、四个 RUNS runtime 源文件和精确两旧点停采/必要绑定迁移。保留原历史、原始化验、冻结 EXPERT8/GPT v2 参数及采集 5 秒、普通历史 60 秒、液位历史 30 秒。部署阶段 EXPERT8.1 曾暂停；用户后来恢复训练并最终明确“最终目的是生产预测”。当前按110个已保存候选的向前日期回测重选三系统S__BOOST_absolute_error_2，二系统保留现用14/15，已于北京时间2026-11-03 15:44:40.311965独立终审后正式封存于production-freeze，未激活。未来对照已核定为EX8_PRODUCTION_RUNS：冻结原参数＋真实RUNS，历史两阶段仍legacy不回填。早先有限轮C7与拟合优先GBR400均保留历史证据；详见下文。未激活或部署研究模型、未开展 DS 清理。

本次整体修订是 `trend-runs-20261103-642b8e234784`。门户下载文件名仍为 `ProjectUpdate.zip`，113,336 字节、26 成员，SHA256 为 `6c9d68262911b1d058e4328f0146fc767357ad9e6dc6bf9c9f4db22508569eab`。RUNS v4 组件 SHA256 为 `3d98f9cb7b99ca5c6593100f405027365c499f94b0fb3cef20d2bade1af7d659`。

三图分别显示小时灰分、A304 设定、本小时建议，共用横轴；密度轴 1.44–1.56、步长 0.02，灰分轴 7.5–11.0、步长 0.25。差值阴影取消，调密记录表保留。脱粉实时、业务及输入取 473/474 RUNS；cutover 之前的精确历史仍读旧 if_prod，跨切换小时或同一路定义混合时不混算运行占比，既有存档快照不改写。

## 上次拒绝的原因与修复

旧 v3 运行在两个组件的共同预检阶段，现场 `derived.py` 不在其有限可信前态集合，预检因此拒绝；RUNS 和前端都未应用。该拒绝本身符合未知源码不得覆盖的规则。

用户运行最小只读采集后，四源码原字节已回传并逐一核验。真实前态证据在 `output/runtime-source-capture-20261103/received/capture-0a5d51cbb52075ee/`。v4 依据该实测前态增加精确 derived/inputs 变体，保留非 RUNS 区段原字节，不放宽未知版本拒绝规则。业务验证、兼容证明与实包独立复核见 `desliming-runs-switch-20261103.md`、`density-trend-runs-deployment-20261103.md` 及对应 output 审计产物。主代理在核验独立结果后给出本次发布 GO。

## 已完成的发布与安装前证据

- 13:10 核对时，笔记本门户 8765 与笔记本 8000 均无监听；没有按端口杀进程。使用已有精确 `backend/scripts/offline_transfer.py` 以 Hidden 启动门户，启动 PID 13704，未启动笔记本采集或 Web。
- 13:15:15 发布 v4，HTTP 下载为 200、113,336 字节，完整 SHA256 与验收候选一致；旧 RUNS 下载仍为 200、25,425 字节。全量安装 `latest.json` 原字节不变。发布证据为 `output/deployment/density-trend-runs-20261103/published-release-report.json`。
- 旧门户 v3 保存在 `outputs/offline-win10-x64/extras/ProjectUpdate-before-trend-runs-20261103-ce0ae2c80476.zip`；这只是分发文件回退，不能当作生产安装回滚。
- 安装前轻量 `/api/v1/health/live` 为 200、`status=ok`。先前误探 `/health/live` 的 404 是路由错误，不是服务故障。
- 安装前三个前端服务字节全部精确匹配旧 hash，目标匹配数 0/3；collector connected、有 owner/未来 lease，最新缓存两批接收时间推进，RUNS 为 Good Boolean。旧两 if_prod 点仍 enabled=1、cutover 尚不存在，说明新切换未应用。当前来源 revision 9 已显式绑定 `desliming_status`，安装时应保留该绑定而非无条件新建 revision。
- 现有 11 个窗口中没有已连接远程桌面；只读桥接不支持安装。不会新建远控配置或绕过只读权限。

上述安装前实测详见 `predeploy-status.json`、`portal-restart-v4.json`、`host-evidence-before.json`，均位于 `output/deployment/density-trend-runs-20261103/`。预部署 live-cache 附件只保留本次四点。

## 用户已经获知的具体运行步骤

在主机打开 `http://10.255.2.6:8765/`，点击“下载本次项目更新包（保留主机数据库）”，重新下载 113,336 字节的新 `ProjectUpdate.zip`，不要复用 `ProjectUpdate (30)` 旧目录。全部解压到一个新文件夹，双击最外层 `UpdateProject.cmd`，等待 `UPDATE INSTALLED`，然后在主机浏览器按 Ctrl+F5。保留解压目录和 `update-logs`。本段记录当时提供的完整步骤，实际执行结果如下。

用户已实际执行新包并明确确认 `UPDATE INSTALLED/更新成功`。主机回传整体 `trend-runs-20261103-642b8e234784` 的 installed/healthy=true，主机时间 13:19:23.875，门户接收时间 13:19:56.007。RUNS 自然等待旧 lease 到期后取得新 owner，两个组件均成功；无强制清除数据库 owner、无按端口杀任意进程。上述步骤现为已完成的操作记录，后续无需重跑安装。

## 安装后实测结果与证据

仅接收本整体 revision 的 `installed` 且 `healthy=true`，不能使用旧 `runs-points-20261102-v1` installed、RUNS 组件成功或单独前端成功代替。门户持续将回执写入 `outputs/offline-win10-x64/target-install-events.jsonl` 与 `target-install-status.json`。

已运行 `output/deployment/density-trend-runs-20261103/verify_host_deployment.py --phase after`，无错误。只调用轻量 health、三静态文件、live-cache，以及四条短主键/最新 revision 查询；事务均关闭。不调用 current/hourly，不扫描历史或读取化验标签。另一次短主键 collector 查询核对持续提交推进，完成后停止扩展检查。

| 验收项 | 实际结果 |
| --- | --- |
| 整体版本 | 精确整体 revision installed/healthy=true；用户确认 UPDATE INSTALLED |
| 三个前端服务文件 | index/JS/CSS 3/3 SHA256 精确匹配目标，详见下表 |
| 健康 | `/api/v1/health/live` 200、ok、v0.3.0；主代理也独立读取成功 |
| 采集器 | connected，新 owner `1a2ad5b33bfa424d851ec8a515889965`，后续 owner 保持且 lease 在未来 |
| 数据提交持续推进 | SQL `last_sample_at` 从 05:19:02.819Z 推进到 05:20:03.031Z；安装回执也确认重启后两次 committed sample 时间增长 |
| RUNS | 473=true、474=false，均原始 Boolean、Good/status 0；两最新批次各自同批同接收时间，05:19:52.944Z → 05:19:57.928Z；key/node/definition_id 保持精确身份 |
| 旧 if_prod 两点 | enabled=0；最新两个活动批次均无旧两点；旧历史保留 |
| cutover | `desliming-runs-v1`、kepserver-main，effective_at `2026-11-03T05:18:15.125Z`（主机13:18:15.125） |
| 显式绑定与其它设置 | 当前来源 revision 9 原记录逐字节保持，desliming 仍绑定 desliming_status，所有其它 choices/目标范围 8.5–9.0 均未改 |
| 周期 | 缓存实测 poll=5、cadence=60；液位30秒由受审安装器配置精确预检与有限差分保留，未通过桥接独立重读生产配置 |

| 前端文件 | 装后服务字节 SHA256 |
| --- | --- |
| index.html | `984e706f503b5fc2375a764f15323b23f897d36e0cbef2ccba13b5b6bfeb4fa8` |
| js/decision-density-trend.js | `3567bddad8ea04f37802c6ded0e7d8132e617abf032aebe74275653aec4d0e96` |
| css/decision-density-trend.css | `0bb4a9d38fbdbe4643983c8b54018eb1c8e143a3e6f903d74cde4ae0b8d308b6` |

装后附件为 `host-evidence-after.json`、`host-evidence-followup.json`、`installation-receipts-final.json`；机器判定逐项写入 `deployment-final-status.json`。附加 compact point-metadata 查询未取到定义原文：传 point key 被400拒绝，改用已知 definition_id 后返回503，因此不将该端点声称为装后成功证据，也不再重复请求。原始 live samples 可直接核对 key/node/definition/status/Boolean/批次身份；安装器在实际安装中已成功解码并校验 compact 定义。此限制不等同于 RUNS 采集故障。

主机四源新 hash 不能由现有只读 API 直接取回，不把三前端 hash 当作后端文件实测。安装器已受审的源 hash 校验、精确变体与安装日志是该项证据来源；如安装出现异常，再定向收日志或四源码，不扩大远程读面。

## 恢复入口与限制

组合先对两个组件都预检，再顺序执行 RUNS 与前端，各有自身恢复流程。任一组件失败便停止后续，部分成功不会报告整体 installed；RUNS 已成功但前端失败时，不自动撤回已完成的 RUNS。顶层结果在解压目录 `combined-update-status.json`，日志在 `update-logs/`。

RUNS 安装备份在安装根 `backups/runs-switch-<时间>/`，含受影响源/配置及 `journal.json`。前端备份在 `backups/source-update-<时间>/`，含 `source/` 和 `files.json`。恢复应先核对 journal、文件/配置/来源 revision 是否有外部变化；程序会在发现外部编辑时拒绝盲目恢复或重启。掉电遗留 prepared/switched-verifying journal 时必须保留并人工核对，不复制旧全库覆盖当前数据。没有额外虚构的“一键全局回滚”入口。

上述自动恢复仅在安装失败处理分支执行。安装成功后没有 `--rollback` 参数或已交付的成功版本一键回退命令；如需人工回退，须先取得实际备份目录及 journal、核对外部变化，再按受影响文件/配置/元数据编制并审查具体恢复步骤。

前端成功回执给出的实际备份目录为 `C:/Users/XMC/DshDensityControl/backups/source-update-20261103-131914-406881`，经既有备份 junction 指向 D 盘数据区。RUNS 成功回执未提供其精确备份目录，保留 `backups/runs-switch-<时间>/journal.json` 模式供现场查找，不将模板或推算时间写成已存在备份。EXPERT8.1 当时暂停及无标签输入证据保留于 `output/gpt-expert81-resume-20261103/`；部署阶段未执行研究流程，后续恢复训练使用独立新目录，未覆盖该历史记录。

## EXPERT8.1 研究续接状态（2026-11-03 用户恢复后）

**当前入口：[生产向重选报告](gpt-expert81-production-selection-20261103.md)。** 最新用户目标是生产预测，以第②项“早日期训练→较新日期”的共同48三系统目标全样本MAE选型，第①项历史拟合仅诊断，第③项未来数据验收。三系统暂选S__BOOST_absolute_error_2（GBR80树、depth2、leaf5、lr0.05、absolute_error，36条strict常规DB），向前全MAE1.980998，对照EX8同48为3.035035；前五差不足0.019pp且描述区间跨零，不能声称可靠唯一最优。二系统保留14/15固定规则，16条仅零训练规则回算；新2系GB只作研究。

当前DB81身份完整分流：70审计目标、6输入合法但标签待核单列回算、5输入/停机/错误时间拒绝；76有成对历史预测。三项完整七指标由comparison-evidence逐条导出，三系统历史58配对全MAE1.628344，对照3.690173；向前48配对覆盖54尝试，6条冷启动；二系规则16/16，所以组合向前64/70。未来仍pending，未显示0误差。ID204/206等本次获授权新捕获标签均已转开发，81身份边界记录于production协议，不能再当未来盲测。

新候选在`output/gpt-expert81-db-primary-20261103/production-freeze/`，核心模型与环境/纯predict已保存并完成正式seal；窗口截止北京时间2026-11-10 15:44:40.311965。三系统只支持实际strict训练四反馈A0/B1/4011/4021，其他三系组合拒绝；四反馈当前恒定，只限定工况，不宣称学会开关独立效应。真正动态建模为液位/煤量。12项定向测试、81行真实纯推理复验通过，guard前后现有预测最大差0；未重训或修改先前两轮锁。主机RUNS切换后的真实生产EX8对照合同已核定：相同c720参数，非2系统（0/1/3/4）按合法RUNS八因素EX8、二系统仅四反馈＋lag90液位走14/15，普通点Good年龄≤30秒。候选原公共六因素/≤65秒近似合同不改，未来各侧独立覆盖、另列合法共同交集。新增路由7项定向测试及真实prepare→copied predict→reveal records→copied score文件链通过；最终runtime/deps已经主代理独立终审，8场景文件链PASS且无阻断；正式seal SHA256为879ec180143034e6f0128a62969aa3160c0da8bce975f9d173b7fd4c13d032d2。拟合GBR400的最终seal已取消，原`final-freeze/`保留。

以下为最早恢复有限轮的**历史记录**，其“未读标签/保留C7”仅指当时，不能当作当前研究状态：

已完成一次离线固定四新候选＋C7/E5控制比较，产物为 `output/gpt-expert81-continue-20261103/`，[完整研究报告](gpt-expert81-continue-20261103.md)。预先锁定早8＋主15共23条开发代理（常规21），尾6及曝光新6仅诊断。四个新候选均未同时满足早段、主段常规命中数及MAE不退门槛，因此保留C7；开发常规11/21＝52.38%、MAE1.433305，80%目标未达成。没有扩大搜索或使用未曝光标签。

严格按首次完整捕获时序审计，旧10月34条全部因合法过去标签不足不可评估，不能把初录created_at代理指标称为独立前向结果。ID204/206仍仅输入证据，标签未读；RUNS与旧if_prod分组不混用。C7/E5旧逐条预测最大复现差0、冻结模型及预测锁hash前后一致，45项针对性及回归测试通过；新状态保存为offline_not_activated。未访问主机、启动采集、修改生产参数或重复部署。

主代理已验收本轮有限训练结论：四候选未同时通过锁定门槛、继续保留C7、80%尚未达成。最终独立审计[终审说明](../../output/gpt-expert81-continue-20261103/audit-final.md)及[机器结果](../../output/gpt-expert81-continue-20261103/audit-final.json)为24项全PASS；496行按保存参数独立公式重构最大差3.55e−15，48项保护输入及11个原训练manifest成员hash一致。终审PASS指本轮产物与结论通过验收，不表示未来准确性达到80%。

正式seal已于北京时间2026-11-03 15:44:40.311965独占持久写入；封存状态仅研究候选、未激活，窗口截止11-10同刻。七日一次性任务`ex8-1`已经工具与本地配置独立核验为ACTIVE，计划北京时间2026-11-10 15:50在同一聊天执行；延迟不移动上述固定168小时窗口。只读本地执行需要本机/Codex运行、已批准主机可达、双方时钟及冻结hash约束满足；先锁无标签输入与预测后揭标签，不训练/校准/部署。receipt及prompt在`output/gpt-expert81-db-primary-20261103/schedule-receipt.json`和`seven-day-task.prompt.md`。未来仍pending、整体目标active，成功复验后停用一次性任务。

## 待办与交付状态

1. 本次主机部署和最低负载装后核验已经完成，无需重复运行安装或扩大检查。
2. 用户浏览器若仍显示旧页面，可按 Ctrl+F5。保留本次解压目录、update-logs 与主机备份用于后续追溯。
3. EXPERT8.1两轮110候选搜索及生产向重选已完成，当前三系S__BOOST_absolute_error_2＋二系现用14/15候选尚未激活/部署；production-freeze已完成最终独立文件链验收及真实UTC独占seal，模型未激活、未部署。未来生产EX8_RUNS合同已明确，不再混同历史legacy。无追加参数搜索/新训练/新数据。168小时新数据窗口已固定为北京时间2026-11-03 15:44:40.311965至11-10同刻，当前future仍pending，输入先锁再揭标签；历史拟合不替代未来验收。页面/API三项指标交付另见对应记录。

机器状态文件为 `output/deployment/density-trend-runs-20261103/deployment-final-status.json`；当前状态 `installed_verified`、`installed=true`。续接应读取该文件与本报告，不把本文安装前证据或旧专项历史记录误认成当前待办。
