# 手工补录修改/删除与近1个月显示 · 2026-10-28

## 功能（用户 2026-10-27 要求）

业务指标补录记录在此前「输入值存库 + 列表显示」的基础上新增：

1. **修改**：记录表每行新增「修改」按钮，弹窗内可改「补录信息名」（现有指标下拉）与「值」
   （按指标单位与 0–100/非负校验），保存写入数据库；补录时间与录入时间保持不变。
2. **删除**：每行「删除」按钮，浏览器确认后从数据库硬删除该行。
3. **近1个月**：记录列表从「全部信息」改为默认只显示近 1 个自然月（按北京时间滚动窗口），
   标题、说明与统计文案同步更新；信息名筛选与分页不变。

## 实现口径

- 后端（backend/app/routers/manual_measurements.py、schemas.py）：
  - `PATCH /api/v1/manual-measurements/{id}`：仅接受 `metric`/`value`（可只改其一，`extra=forbid`）；
    `metric` 限定现有可录入指标（不得新增枚举，避免破坏更新包兼容）；沿用与新建一致的
    数值/单位/名称派生；不改 `observed_at`/`created_at`；保留原 POST 幂等 `request_hash`
    （编辑后原提交重放仍复用该行并看到编辑后的内容，不同内容仍 409）。
  - `DELETE /api/v1/manual-measurements/{id}`：硬删除（用户明确要求含数据库）；重复删除 404。
  - `GET` 列表新增可选 `start`/`end`（观测时间闭区间，按北京时间无时区可、Z 或偏移均可，
    规范化到与库内一致的 UTC 串）；不传时行为与原先完全一致，其他调用方不受影响。
  - 写端点全部挂 `require_write`（token / 本机回环），只读测试端同样被拒。
- 前端（manual-measurements.js、api.js、manual-measurements.css、index.html）：
  - 操作列按钮仅在可写模式渲染；修改弹窗为独立 `<dialog>`（深浅色同步），原记录摘要展示
    不可修改的时间；删除走 `window.confirm`。
  - `windowStart()` 用 `Intl` 按北京时区做自然月回退（含月末日期钳制），随每次加载滚动。
  - `api.js` 数据层支持 `PATCH`/`DELETE`（写守卫 + token），并对返回体做严格确认（id/值/时间
    齐全才算成功，避免把异常响应当成功）。
  - 保存/修改/删除后沿用既有联动刷新（重介灰分变化触发相关视图）。

## 验证

1. **pytest**：`tests/test_manual_measurements.py` + `tests/test_auth.py` 共 57 项全过，其中新增
   8 项：改信息名+值落库、只改值/只改名、校验与 404、遗留粗精煤泥量行不可跳入百分比指标、
   编辑后幂等重放语义、硬删除、远端写鉴权（401/正确 token）、观测时间窗（含边界闭区间、
   无窗参数行为不变、非法时间 422）。相关回归 `test_readonly_operations`/`test_hourly_quality_advice`
   53 项通过。
2. **真实浏览器端到端**（Chrome headless，隔离临时库本地服务 127.0.0.1:8017，未触碰主机与
   笔记本生产库）：`backend/scripts/verify_manual_measurements_edit_browser.js` 通过——
   预置 2 个月前旧记录被近1个月窗口排除（API 无窗可见、窗口内 0 条）→ 界面新增 8.5 记录并
   显示「近1个月共 1 条」→ 修改为浮精灰分 10.5（数据库核对 metric/value/name）→ 删除后
   界面清零且数据库仅剩旧记录；全程无页面错误/控制台错误。截图与报告：
   `output/playwright/manual-measurements-edit-20261028/`（after-open/after-save/edit-dialog/
   after-edit/after-delete.png + report.json，本地验证产物未纳入提交）。

## 未覆盖项

- 主机生产页面实际操作：本批仅落仓库，未部署主机（按项目规则需另行打包授权）。
- 只读测试端上「修改/删除」按钮按设计不渲染（`writable()` 判定），未单独截图验证。
