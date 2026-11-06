# 工作目录与规则

本目录（`D:\dense-medium-density-control-system\frontend`）是「重介密控系统」的**前端**目录，
与后端 `backend/`、文档 `docs/` 同属一个 Git 仓库（`D:\dense-medium-density-control-system`）。

> 历史说明：本文件原先指向 `D:\Users\liu\Desktop\测试\web (2)`，并声称那是唯一允许修改代码的地方。
> 项目已整合到上面这个仓库，桌面旧副本**不再是**工作目录，不要往那里写改动。

## 硬性规则

1. 只在仓库内修改文件；后端改动落 `backend/`，前端改动落 `frontend/`，
   文档与 Excel 产出到 `docs/`（Excel 一律由 `backend/scripts/generate_*.py` 生成，便于复现）。
2. 不要改动以下目录/文件，除非用户明确另行要求：
   - `D:\桌面\重介密控`（含其 `web`、`data`、`.exe` 等）
   - 桌面上的旧副本、其它任何副本或旧版本目录
3. **`?v=N` 版本号建议顺手 `+1`，但已不再是必需**（2026-09 起）。
   原因：后端已对静态资源统一发 `Cache-Control: no-cache`（`app/main.py` 的
   `no_cache_static_assets` 中间件），浏览器**每次都必须校验**——文件没变走 304（几十字节），
   文件变了立刻拿到新的。所以即使忘了改版本号，改动的 JS 也会生效。
   保留版本号作为"双保险"没有坏处；但只要别把它当成必须完成的动作，
   就不会再出现"忘了 bump → 以为没生效 → 反复排查"的浪费。
4. **同一时间只允许一个 agent 持有未提交改动。** 多 agent 并发时，`git add -A` / `git commit -a`
   会把另一个 agent 改到一半的文件一起提交，也会把互不相关的改动裹进同一个提交。
   开工前先 `git status` 确认工作区干净；收工前确认自己的改动已单独提交。

## 常用说明

- 当前项目是「重介密控系统」前端（原生 JS + chart-lite，无构建步骤）。
- 页面代码约定：
  - `coarse.js`：粗精煤泥灰分页
  - `float.js`：浮精影响分析页
  - `decision-overview.js`：决策总览核心界面
  - `manual-measurements.js`：总览独立补录弹窗
  - `app.js`：全局数据 store 与模型
  - `api.js`：双轨数据层（`file://` 仅 localStorage；`http://` 额外整库镜像到后端 `/state`）
- 同一批数据按不同生产系统重复导入时，同一采样时间只应显示/统计一条（已用 `_uniqueByTime` 处理）。
- 前端与后端在算法/取值链上必须**逐值一致**（golden 对拍 <1e-6）：
  `backend/app/services/*.py` 与 `frontend/js/app.js` 是同一套公式的两份实现，
  改动任一侧都必须同步另一侧，并用 `backend/scripts/dump_*_js.js` 系列对拍验证。

## 视觉检验（用户 2026-11-06 明确要求）

改完任何看得见的东西（页面、卡片、图表、配色、布局），**交付前自己先出图看一遍**，再报告完成：

1. 截图：`chrome --headless=old --hide-scrollbars --virtual-time-budget=<ms> --screenshot=out.png <url>`；
   要截非首页（密度趋势、维护页、弹窗）就写一个临时同源页 `frontend/__x.html`，iframe 载入 `/index.html`
   后在 iframe 里 `click()`/点开对话框，截完删掉该临时文件（不要提交进仓库）。
2. 用 `read_image` **亲眼看**这张图，然后判断：该有的都在、不该有的没留、刻度和文字没互相压盖、
   曲线形状符合物理含义（阶梯就是阶梯、该断的断）、深浅色下不塌。
3. 数字对 ≠ 看起来对。像素探针（同源 iframe 读画布 / 内部状态）用来量化，但不能替代看图。
   **canvas 图表另加一条硬断言**：改完 dump 一次绘制路径坐标（hook `ctx.moveTo/lineTo/stroke`），
   校验 `x` 从不后退、没有 `null/0` 坐标、x 落在绘图区内。2026-11-06 的 A304「乱成一团」，
   像素覆盖率是 100% 却完全错——因为 `lineTo(null, y)` 被 canvas 当成 0，线从最左边画回来。
4. 图不对就改，改完重新出图重看。报告里写「图上实际看到了什么」，并写明取舍与副作用
   （例：为让曲线连成一条线，牺牲了从该曲线看出停机位置的直观性）。
5. 本机页面服务：`backend/scripts/start_server.ps1` 起在 `http://127.0.0.1:8000/`（静态文件按请求读盘，
   改前端不用重启）。
