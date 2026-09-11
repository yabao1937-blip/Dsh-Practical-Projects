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
  - `import.js`：批量数据导入页
  - `app.js`：全局数据 store 与模型
  - `api.js`：双轨数据层（`file://` 仅 localStorage；`http://` 额外整库镜像到后端 `/state`）
  - `xlsx-lite.js`：无依赖的 xlsx 读取器（浏览器侧解析三表）
- 同一批数据按不同生产系统重复导入时，同一采样时间只应显示/统计一条（已用 `_uniqueByTime` 处理）。
- 前端与后端在算法/取值链上必须**逐值一致**（golden 对拍 <1e-6）：
  `backend/app/services/*.py` 与 `frontend/js/app.js` 是同一套公式的两份实现，
  改动任一侧都必须同步另一侧，并用 `backend/scripts/dump_*_js.js` 系列对拍验证。
