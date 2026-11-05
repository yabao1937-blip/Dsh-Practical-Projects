# 粗精煤泥模型左右对比页面验收（2026-11-03）

> 当前交付已完成：最终候选按历史时序回测选择并正式封存，API 为 ready，freeze 与 candidate 均 accepted。三个时段完整显示 MAE、RMSE、Bias、R²、±1 pp 达标率、N、覆盖率及实际日期。普通无 route 浏览器与完整 App 的正式封存导出复验均通过。原 GBR 拟合优先截图仅在 `output/coarse-model-comparison-ui-20261103/history-fit-first/` 保留为历史证据，不是最终候选。

正式封存窗口：2026-11-03 15:44:40 至 2026-11-10 15:44:40（北京时间，168 小时）。页面精确显示到秒。候选尚未替换生产，未来七项指标及共同样本对照均待验证，不把封存当作未来验证完成。

正式普通浏览器展示图：`output/coarse-model-comparison-ui-20261103/preview-presentation.png`（桌面，外层 24 px 留白）、`preview-desktop.png`（含本地只读标记）、`preview-mobile.png`（390 px）、`preview-system2.png`。这些文件现在均来自 accepted 导出。`preview-verification.json` 记录 passed=true、noRouteInterception=true、ready/accepted、6 次本机 GET、errors=[]，并记录页面实际显示的北京时间窗口。

完整标识：artifact `c56e74f819b9ff1f689fbab47463019c49db314168a236bcfd070e92679d8c9e`；model `b768cb2a8b2b8c37b67f76030ad0aeb535afb69d5b71230c4fe2d3facac5a2e0`；runtime `602dd123b7bc274cebfdea8d9923b06287e7d875a8875670f0296b955e7d3975`；final seal `879ec180143034e6f0128a62969aa3160c0da8bce975f9d173b7fd4c13d032d2`。真实 GET 快照为 `preview-served-comparison.json`。

新增口径：三个时段日期可能重叠，不视为互斥日期段。每阶段覆盖率严格读取该阶段的 `eligible_n/coverage`，不复用数据库全量分母。Bias = 平均（预测 − 实测），正负号始终明确；R² 使用普通数值，可为负，样本不足或真实值无方差时显示“不可计算”；全部未来字段尚未验证时显示“待验证”，不能以零代替。冻结现用模型的历史同池回算不写成逐折重训；保留的固定 14/15 规则不虚构训练日期。

未来验证另按各模型独立状态展示：一侧拒绝而另一侧能预测时，不把两边同时隐藏。已经有揭标签目标、某模型 0 条可评分时显示“未能评分”、真实 N=0、真实 0% 覆盖与拒绝原因；这区别于尚未开始的 pending。未来共同样本公平对比使用 `future_comparison.by_system` 的交集，单侧拒绝仍保留在上方各模型自己的覆盖分母中。全系统的尝试预测覆盖（可含未揭标签）单独标“全部系统”，不挪作某系统的可评分覆盖率。定向浏览器状态夹具已覆盖上述单侧拒绝、另一侧 available、共同交集无样本和共同交集已可评分的状态。

三系统未来现用对照显示 API 的 `baseline_id/baseline_label`，即 EX8_PRODUCTION_RUNS 生产 RUNS 输入、参数保持冻结；历史两阶段保留 native legacy 输入，不回填重算。

未来范围包括 0/1/4 系统及未知工况。界面不新增主标签页，而在未来共同样本区域读取实际 API 分组的 `eligible_n`，单列这些其它工况的已揭标签目标总数与逐组数，并说明不属于当前二/三系统标签页、仍完整进入全系统预测覆盖。无未来样本仍显示“范围：待验证”。定向 0/1/4 三条 old-only 状态夹具通过：现用全系统 100%、候选 0%，其它工况总数 3 与每组 1 均保留，默认三系统不会冒用这些分数。

- 模式：Impeccable Operate；本次是现有页面的局部扩展，沿用深色监控界面的字体、变量、按钮和导航。子代理按自包含任务书直接实施，未另作设计访谈。
- 生产组件入口：原“粗精煤泥分析”导航（`frontend/index.html` → `page-coarse`）。`8766` 只用于拦截 API 的完整 App 集成测试，不作为普通浏览器展示交付。
- 只读接口：`GET /api/v1/training/coarse-model-comparison`。名称、方法、分数与候选状态取正式 API；前端不计算训练、不保存参数、不提供激活或部署按钮。
- 修改：新增 `frontend/js/coarse-comparison.js`、`frontend/css/coarse-comparison.css`；`coarse.js` 刷新时加载对比；`index.html` 接入资源与语义区域；旧专家组件保留图表和数据详情，原训练指标默认折叠并标注“旧训练集参考”。未改变预测参数或公式。

## 展示口径

宽屏左右并列，三行依次为历史全量回算 / Historical Evaluation · MAE、历史时序回测 / TSCV · MAE、冻结后未来验证 / OOT · MAE。默认三系统，二系统独立切换；二系统左侧为实际 14/15 规则，不能冒充 EX8 算法结果。三个主值均为全样本 MAE，单位 pp（灰分百分点），辅助显示 ±1 pp 达标率、命中数/评分数、N 与实际覆盖。常规样本仅展开补充，不覆盖主值。无系统分组时不回退到总体指标。

历史回算包含开发数据；TSCV 只使用较早日期训练后预测较新日期，不是留一日期验证或独立盲测；同日化验未默认提前可用。未来无合格新样本时显示“待验证”。缺失、过期、校验失败和接口错误均不以零或旧值代替；网络错误提供重新读取。

## 验收方式与证据

### 可直接打开的本地只读展示

普通浏览器访问 `http://127.0.0.1:8767/`。独立展示壳 `frontend/coarse-comparison-preview.html` 加载同一份生产比较组件与样式，并明确标注“本地只读预览 / 不连接现场”。`frontend/tests/serve-coarse-comparison-preview.py` 仅绑定 `127.0.0.1`，只提供白名单页面、CSS、JS 与比较 GET；POST/PUT/PATCH/DELETE 均返回 405，不加载 App 启动、整库镜像、模型训练或采集器。

比较 GET 直接复用 `app.services.coarse_model_comparison.load_comparison()`，只读取并校验固定受控 JSON；每次 GET 重新载入该模块，避免这次开发期间 schema 更新导致预览缓存旧类。刷新按钮即可看到最新导出与真实封存时间。没有 Playwright route、mock 数据或请求注入。

普通浏览器验证脚本 `frontend/tests/verify-coarse-comparison-preview.cjs` 已实开该 URL，核对两个系统全部指标和每时段覆盖分母，记录所有浏览器请求均为本机 GET，pageerror = 0。机器证据 `preview-verification.json`，服务实际响应 `preview-served-comparison.json`；截图 `preview-desktop.png` / `preview-mobile.png`，带外层 24 px 留白的展示图为 `preview-presentation.png`。

启动：

```powershell
backend/.venv/Scripts/python.exe -u frontend/tests/serve-coarse-comparison-preview.py --port 8767
```

本次精确进程身份保存在 `output/coarse-model-comparison-ui-20261103/preview-server.json`：监听服务 PID `79176`，父进程 `41784`，执行命令为 Python 运行本仓库 `frontend/tests/serve-coarse-comparison-preview.py --port 8767`。只在核对仍为该命令时停止，避免 PID 重用：

```powershell
$previewProcess = Get-CimInstance Win32_Process -Filter 'ProcessId = 79176'
if ($previewProcess.CommandLine -like '*D:/dense-medium-density-control-system/frontend/tests/serve-coarse-comparison-preview.py --port 8767*') {
    Stop-Process -Id 79176
}
```

### 完整 App 与故障状态验收

浏览器脚本 `frontend/tests/verify-coarse-comparison.cjs` 使用项目现有的 Playwright 安装和真实 Chrome。以本地静态服务加载真实前端，拦截所有 API；比较接口注入本地导出 JSON，其余 API 禁止写入，原专家图表使用仓库冻结的 `backend/models/gpt_expert8/20261012/production.json` 兼容响应。没有访问生产服务、数据库或采集器。

复现（先在另一终端启动 `python -m http.server 8766 --bind 127.0.0.1 --directory frontend`）：

```powershell
$env:DMCS_PLAYWRIGHT_MODULE='C:/Users/25925/AppData/Local/npm-cache/_npx/31e32ef8478fbf80/node_modules/playwright'
node frontend/tests/verify-coarse-comparison.cjs
```

已验证：动态模型名称；两系统对应的真实字段选择；数值四舍五入和命中率；全样本与常规样本的主次；未来缺结果；校验失败时清除数值；网络错误与重试；方法文本 HTML 转义；旧训练集指标默认折叠；原图表仍呈现；1440 px 左右三行对齐；390 px 单列堆叠、区域无横向溢出、控件可操作；无浏览器 pageerror。

截图及机器检查记录：`output/coarse-model-comparison-ui-20261103/`，其中 `browser-verification.json` 记录运行输入、检查结果与截图。开发状态夹具明确标注“交互验收专用状态夹具，不是真实模型指标”，不可作为模型表现证据。

正式验收已通过。输入为 `backend/data/gpt_coarse_model_comparison.json`，完整 App 浏览器实际使用的快照为 `output/coarse-model-comparison-ui-20261103/browser-used-comparison.json`。三系统历史回算 EX8 / 最终候选 MAE 为 3.69 / 1.63 pp（58/60 条），时序回测为 3.04 / 1.98 pp（48/54 条）。二系统保留实际 14/15 规则，时序同池回算 16/16；整体历史 76/81、时序 64/70，未来均待验证。全部七项指标、日期及逐阶段分母已逐值核对。可回算 76 条与已审计训练资格 70 条分开；6 条待核对仍单列回算，5 条无法预测的原因按接口披露。

机器检查通过，浏览器 pageerror = 0。最终完整 App 使用 `DMCS_COMPARISON_REAL_ONLY=1`，只重验 accepted 真实导出、左右行对齐及手机交互，没有重复状态夹具。既有状态验收证据保留在 `expanded-metric-state-checks/`。最终日期已由真实封存数据驱动显示为 11 月 3 日至 11 月 10 日北京时间 15:44:40，未来依然待验证。

正式截图：`page-desktop.png` 含 1440×1600 的外层导航、留白与原专家组件；`comparison-desktop.png` 为比较区；`comparison-system2.png` 为二系统；`comparison-mobile.png` 和 `page-mobile.png` 为 390 px 视图。`error-desktop.png` 为独立网络故障状态。截图中的真实比较数据均来自本次导出，但展示环境仍是本地静态预览与拦截 API，不能称为现场服务截图。

## 设计检查与限制

已人工查看桌面和小屏实际截图，修正了小屏页面继承的 1200 px 最小宽度，并使两列三行保持对齐。新增 CSS 的 Impeccable 检测没有确定性问题。`index.html` 扫描报告的 20 处灰色/对比度问题来自既有全站内联样式；本次未新增该类内联样式，未扩展为全站改版，未添加任何忽略规则。

现有全站顶部导航仍按工业桌面监控布局；本次保证模型比较区域及其控件的小屏适配，不重做全站导航。模型真实冻结或未来七天验证是否完成以 API 证据为准，本次 UI 不推断或修改这些状态。
