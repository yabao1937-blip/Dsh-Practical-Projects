# 决策总览刷新过渡验证

2026-10-08，针对实时 GPT 链路周期刷新导致的闪断：

- 旧实现清空结果后立即全量替换画布。新实现仅同步变化的文本/属性，保留卡片、SVG 路径、动画实例和动画延时；数字用 220ms 轻微淡入，不插值。
- 本轮结果在输入变化时仍立即失效。独立展示层仅在输入状态合格时短暂显示上一轮结果，标“更新中／上轮结果”，`available=false`；预览最多 5 秒，上一轮结果不得超过 30 秒。停机、沿用、缺失、过期或请求失败撤下旧预览。
- 尺寸未变时跳过画布重新适配；关闭的详情不重建。打开的详情保留节点、焦点及滚动，切换卡片显式同步下拉框实际值。
- 生产分支保留原渲染路径；计算、采样频率、后端和 DS 模型均未改。

验证结果：

1. `frontend/tests/decision-refresh.html` 浏览器隔离测试 10 项通过：连续 50 次更新保持卡片/数值/SVG 节点身份、保持动画实例、选点与焦点、滚动、真实数字、没有移除持续节点、切换卡片来源正确、减少动态模式、切换生产后恢复 GPT。
2. `node backend/scripts/verify_decision_realtime.js`：包括旧请求丢弃、预览不可作为当前建议、预览限时、失败/失效立即清除。
3. 既有 `verify_decision_overview.js`、`verify_decision_heavy_amount.js`、`verify_manual_measurement_points.js`、`verify_telemetry_retained.js` 均通过。
4. 原取值链与 DS 训练对拍 22 项通过（`test_resolvers.py test_training.py`）；`train_js.json` SHA256 仍为 `AA09B7A4574EC19BE5F840B68F3611394B0584F2EC435B0ABD062416F8566BBD`。
5. 同 Wi-Fi 实际页面正常计算和刷新。前端静态资源热更新，刷新浏览器即可，无须重启采集或后台。
6. 实际页面浏览器错误日志为空，截图见 `refresh.png`。截图时煤量不平衡且密度沿用，建议正确显示“暂缓／—”，未用过渡掩盖输入异常。
