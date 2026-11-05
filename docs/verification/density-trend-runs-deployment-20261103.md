# 2026-11-03 密度趋势与脱粉 RUNS 组合交付准备

当前状态：**主机部署完成。准确整体 installed/healthy=true、用户成功确认及装后最低负载核验均通过。** 修订 trend-runs-20261103-642b8e234784；详见文末最终部署记录及 host-deployment-handoff-20261103.md。

## 已确认的范围

- 前端只更新 `frontend/index.html`、`frontend/js/decision-density-trend.js`、`frontend/css/decision-density-trend.css`。
- 三图分别显示小时灰分、A304 设定、本小时建议，共用横轴。密度范围 1.44–1.56、步长 0.02；灰分范围 7.5–11.0、步长 0.25；取消差值阴影，保留调密记录表。
- 为避免顺带部署局部 DS 清理，发布用 index 由主机原始源文件只替换趋势容器、移除差值图例、更新 CSS v7/JS v11。非趋势字节与主机相同，现有 DS/GPT 控件保留。JS/CSS 与本次已验收工作区字节一致。
- RUNS 切换由独立 `host_runs_switch` 组件负责；前端组件不承担配置、绑定、数据库或采集切换事务。
- 整体使用一个最外层 `UpdateProject.cmd`，两组件先分别预检，然后 RUNS、前端依次执行。保留各自事务和失败恢复，不引入全局数据库备份或全局回滚。
- 采集 5 秒、普通历史 60 秒、液位历史 30 秒，原始化验、历史数据、配置其他字段、冻结 GPT 和离线研究均不纳入前端发布。

## 主机真实前态

主机 `http://10.255.2.7:8000`，安装根为 `C:/Users/XMC/DshDensityControl`。本轮只读取静态前端资产，没有调用 current/hourly 或扫描数据库。

首页根路径 `/` 在后端动态注入认证元数据并重读文本，不能作为原始文件哈希。本轮补取 `/index.html` 获得源字节；私有根响应不进入任何交付包。原字节、响应头、耗时记录在 `output/deployment/density-trend-runs-20261103/host-baseline/`。

| 文件 | 实际前态 SHA256 | 发布目标 SHA256 |
| --- | --- | --- |
| index.html | `4d8e9ef31b66f7881cac11827b757168568fa02a7056e85867d56747ba2f11ef` | `984e706f503b5fc2375a764f15323b23f897d36e0cbef2ccba13b5b6bfeb4fa8` |
| decision-density-trend.js | `e5f261ed36b8c4392e08dce3364a9ad2b1cfa84c9ac632181e11eb05794a926d` | `3567bddad8ea04f37802c6ded0e7d8132e617abf032aebe74275653aec4d0e96` |
| decision-density-trend.css | `141a111d2dc0a4930646f4eaba29f9865be7abcd7821047311e02081482e9040` | `0bb4a9d38fbdbe4643983c8b54018eb1c8e143a3e6f903d74cde4ae0b8d308b6` |

主机 index/JS 与历史 `ProjectUpdate-6dd6411.zip` 目标字节匹配；CSS 与更早 `ef933c7` 匹配。安装守卫使用本次真实字节，不推定整机代码等于笔记本 HEAD。

## 前端专用安装分支

沿用现有 `query-only-hotfix`、备份与原子源文件替换流程，新增显式 `frontend_asset_verification=density-trend-three-panels-v1`：

- 文件集合、payload 目录树必须精确等于三个静态文件；每个文件必须有严格的旧、新 SHA256，禁止扩大 accepted 前态列表。
- 拒绝历史间隔、schema、spool、函数补丁等字段与此分支混用。
- 预检通过前不创建备份、不改源、不启停服务。`--preflight` 不联网。
- 只备份三个旧源文件，不复制或恢复数据库，不停止 collector。
- 正常安装及失败恢复都用 `health/live`。装后直接核验 `/index.html`、JS、CSS 的服务字节 SHA256，不触发 current/hourly 或普通 DB readiness。
- 没有显式 flag 的既有更新默认行为保持不变。
- 组合调用时使用 `--component`，组件成功回调只报 copying/healthy=false；顶层只有在两个组件全部成功后才报 installed/healthy=true。

精确归属的停 Web helper 复用 RUNS 组件已封存版本 `28173f157366e76f5b5a02f95def5c8a29c9b4eb1763821a7ee522481fd2ae52`，不使用旧的命令行子串归属判断。

## 前端组件与本地证据

- `output/deployment/density-trend-runs-20261103/FrontendDensityTrend-20261103.zip`：41,370 字节，SHA256 `6f34e86beb9288a68ce5c142945fdc34ee87393fb954650870bd735999fe7427`。交付 CMD 已规范为 CRLF；安装器和三文件 payload 字节未因规范行尾而变化。
- `frontend-component-package-report.json`：ZIP、安装器、目标与前态完整哈希；`frontend-component-manifest.redacted.json` 隐去既有私有回调地址。
- `frontend-stage/scope-report.json`、`index-host-to-payload.diff`：主机非趋势字节守恒、JS/CSS 与工作区一致。
- `frontend-coordinator-tests.log`：前端安全负例、正常/重复安装、静态/启动失败回滚、组件回调、串行步骤/失败中止/协调锁及既有安装回归，共 170 项通过。
- `independent-frontend-stage-audit.json`：独立代理 9 场景隔离演练通过，禁止 SQLite、collector 和 current/hourly，保护 DB/配置/模型/RUNS 哨兵保持原字节。
- `independent-frontend-package-acceptance.json`：最终前端 ZIP 13 项检查通过，含精确成员/来源、实际 CLI 预检与组件回传、CMD CRLF；结论绑定上述 `6f34e86b…` ZIP 哈希。
- `independent-coordinator-stage-audit.json`：串行协调器 8 场景独立隔离演练通过，覆盖两预检优先、任意失败中止、部分成功不报整体成功、foreign 锁不动、进程启动失败和损坏包前置拒绝。该 Python 源文件 SHA256 为 `5849db67d6dfc6dcd41c0e0d8d9d7bb0d88d569eb428203b0c99818e51e6d192`。
- `independent-combined-package-acceptance.json`：最终 v3 组合 ZIP 15 项来源/结构检查通过，前端 7 成员、RUNS v3 的 13 成员、协调器及共享 helper 均精确匹配已审字节；RUNS 专审同时签字实际 ZIP 的 42 项检查（含三前态正常/重复安装及 Web 健康后采集退出的完整恢复）。

## 已发布内容和待主机安装

组合候选为 `output/deployment/density-trend-runs-20261103/ProjectUpdateTrendRuns-20261103-6e3782381728.zip`，103,089 字节、24 成员，SHA256 `ce0ae2c80476fd7cbe0a306b52d9fc631c703b69f1e437ce2b795fac4378148a`，修订标识 `trend-runs-20261103-6e3782381728`。展开目录为 `combined-stage-6e3782381728/`，每个交付文件在根 `bundle.json` 中登记哈希。

其中前端 7 成员逐字节来自已审组件 ZIP；RUNS 13 成员逐字节来自候选 `output/runs-switch-20261103-candidate-v3/SwitchDeslimingRuns.zip`，55,624 字节，SHA256 `8eb3650f51e84c135a078043cec455fb61fa270ac1f738ff8d9a7702960d3ba7`。RUNS 组件只部署四个 runtime 文件（derived、hourly_brief/inputs、decision_sources、decision_hourly/history），额外两个 derived 变体精确保留 6dd6411 主机旧 PRESS 别名及原 LF/CRLF，只改脱粉常量；具体迁移与历史保真由 RUNS 专审负责。

下载页为 `http://10.255.2.6:8765/`，点击“下载本次项目更新包（保留主机数据库）”获得 `ProjectUpdate.zip`。用户需在主机全部解压到新文件夹，双击最外层 `UpdateProject.cmd`；显示 `UPDATE INSTALLED` 后在浏览器按 Ctrl+F5。包内所有 CMD 为 CRLF。

发布前门户项目更新包为 32,551 字节，SHA256 `f64398ae7aa10cf8e7ca3c2b2365f271e6dca34ab08d21a1336084f17320df3a`，已保留为 `extras/ProjectUpdate-before-trend-runs-20261103-f64398ae7aa1.zip`。版本档案为 `extras/ProjectUpdateTrendRuns-20261103-6e3782381728.zip`；固定 `extras/ProjectUpdate.zip` 已原子替换为同字节。

发布时 8765 已无监听且无门户 Python 进程，故没有按旧 PID 文件杀进程。通过 Hidden 新启动本工作区门户，启动器 PID 66012、真实监听 PID 69452；已核对精确脚本和进程血缘。笔记本 8000 的原只读 Web PID 54464 前后保持相同，没有触碰主机生产服务。全部既有路由保留；门户项目说明已修正为三图/RUNS、按组件预检与按需备份，不再声称全库备份或点位设置不变。

HTTP 已核对主包 200、103,089 字节及完整 SHA256 与已验收组合 ZIP 一致；旧 `/runs-points.zip` 仍为 200、25,425 字节，SHA256 `baa6bac89d74b5a39e5f3fb82ac9c87ce244ee53e3ba0a0fb38fd16e2a771d90`。门户新说明、项目更新链接、旧 RUNS 链接和入口 CMD 名均已通过。全量安装器 `latest.json` 原字节未改。发布明细在 `published-release-report.json`，启动进程明细在 `portal-start-report.json`。

门户持续把主机回执写入 `outputs/offline-win10-x64/target-install-status.json` 和 `target-install-events.jsonl`。当前最后一条 installed 是旧的 `runs-points-20261102-v1`，不能当作本次安装成功；本次应等待整体修订 `trend-runs-20261103-6e3782381728` 的 installed/healthy=true。RUNS 组件标识为 `desliming-runs-v1`，前端为 `frontend-density-trend-20261103-d401209e204a`，组件成功不提前宣布整体成功。`installation-receipt-baseline.json` 已记录当前零条本次回执、installed=false。

### 用户首次执行的实际结果

2026-11-03 10:27:19，整体修订 `trend-runs-20261103-6e3782381728` 回传 failed：`runs preflight failed. No component was applied.` 用户界面给出精确原因 `Unknown installed source version: backend/app/telemetry/derived.py`，未覆盖任何文件。RUNS 和前端应用均未开始；主机健康接口经主代理确认仍为 200。

此前旧诊断回传来自 10-24，仅含数据库/WAL/SHM统计，无四个 runtime 源码哈希或原字节；前端基线也不能证明后端源码版本。因此于 11:00:40 发布最小只读源码核对包，复用既有 `diagnostics.zip` 和诊断上传端点，不扩展通用诊断。用户已运行并成功自动回传。该工具不读取数据库、现场配置、模型或日志，不启停服务、不改安装文件；只有四源码、所选安装根与 D 盘安装标记存在性、SHA/BOM/行尾及压缩原字节。

### 实际四源码回执

核对包 `output/runtime-source-capture-20261103/RuntimeSourceCapture-20261103.zip` 为 3,558 字节，SHA256 `4f9f5fbcaff78ba1547cdf00740f445b7a0f15053ddd2b3532cc1108649a1f4a`，已通过 7 项基础验证和 4 场景独立验证。HTTP 下载字节一致；只替换一键诊断包，没有重启门户、改变 v3 安装包或生产服务。

回执客户端时间 11:02:38，笔记本接收时间 11:03:10（北京时间）。所选安装根明确为 `C:/Users/XMC/DshDensityControl`，`D:/DshDensityControl` 安装标记不存在，与失败组合 CMD 的选择算法一致。报告 kind、精确 schema、时间、XMC 安装根、四路径、压缩大小/解压上限、原 SHA/size/BOM/行尾已逐项核验；未导入或执行采回源码。

| 主机实际文件 | 原 SHA256 | 字节 | 行尾 |
| --- | --- | --- | --- |
| telemetry/derived.py | `1a00abe3e7b0278baffa2cd3052f401381fc4be8d3073fe9278d25e6d6dea34e` | 8,130 | CRLF 131 |
| hourly_brief/inputs.py | `1c03e3589ab56c7e75367ee1895ef83f8e95df5d51acda943c6be2bc6cbaa819` | 18,861 | LF 311 |
| services/decision_sources.py | `780c3a57e2dba3c09e5473c3ac9a40a8c138a6f50d691aa80344d5ecfdab65e4` | 13,168 | LF 221 |
| decision_hourly/history.py | `6c498be210e4fa1dcd2a2c50b913b9915523815dfd3daaab1a6daca5e22cfe9f` | 10,994 | LF 176 |

四文件均无 UTF-8 BOM，原始总计 51,153 字节。证据位于 `output/runtime-source-capture-20261103/received/capture-0a5d51cbb52075ee/`：`capture-report.json` 为收到的原 JSON，23,446 字节、SHA256 `0a5d51cbb52075ee5e2b1ddf683bbe7825ec60cda79e9571eafcf95e56a68a51`；`sources/` 保留四原相对路径下的原字节，`source-summary.json` 为核验摘要。路径与原始报告哈希已同时交 RUNS 实现、独立复核和主代理。

### 按实际前态重封的 v4 候选（尚未发布）

RUNS 实现方已提供 `output/runs-switch-20261103-candidate-v4/SwitchDeslimingRuns.zip`，65,654 字节、15 成员，SHA256 `3d98f9cb7b99ca5c6593100f405027365c499f94b0fb3cef20d2bade1af7d659`。新增 `variants/derived-host-1a00abe3.py` 与 `variants/inputs-host-1c03e358.py`，针对已采回实际字节制作兼容路径，保留非 RUNS 区域；原 13 成员中的 helper/CMD/四主 payload/两个旧变体未改，匹配器、说明及 manifest 更新。具体保真证明在该候选目录的 `host-source-compatibility-proof.json`，业务与 actual ZIP 仍由独立复核验收。

已重封组合候选 `output/deployment/density-trend-runs-20261103/ProjectUpdateTrendRuns-20261103-642b8e234784.zip`，113,336 字节、26 成员（25 哈希），SHA256 `6c9d68262911b1d058e4328f0146fc767357ad9e6dc6bf9c9f4db22508569eab`，修订标识 `trend-runs-20261103-642b8e234784`。15 个 RUNS 成员与上述 v4 源 ZIP 逐字节相同；前端七成员、协调器及根 CMD 保持已验收字节，根 README 仅新增按实际源码匹配、未知版本继续拒绝的说明。

此 v4 只为候选，等待 RUNS 专审、组合增量验收及主代理核对。门户 `ProjectUpdate.zip` 当前仍是此前被预检拦截的 v3，不把旧包标成已修复；用户不应重试旧 v3。新兼容包尚未发布，主机仍未更新。

发布准备方没有执行生产写入；本次只有预检失败回执，不能把发布成功或旧 installed 回执写成主机更新成功。


## 2026-11-03 最终主机部署记录

2026-11-03 13:19:23 主机整体 trend-runs-20261103-642b8e234784 回传 installed/healthy=true（13:19:56 门户收到），用户确认 UPDATE INSTALLED。装后 health200、三前端 SHA 全匹配；collector 新 owner connected/未来 lease，后续 SQL last_sample_at 从05:19:02.819Z推进到05:20:03.031Z；RUNS两点Good Boolean同批推进，旧两点disabled且活动批次不再含旧点；cutover05:18:15.125Z，来源revision9及原显式desliming_status绑定/其它choices/目标范围保持。5/60秒缓存实测；30秒配置及四源hash以受审安装器精确校验为证据，现有桥接不能独立读回。完整交接与限制见 host-deployment-handoff-20261103.md；EX8.1及其他研究保持暂停，笔记本8000未启动。
