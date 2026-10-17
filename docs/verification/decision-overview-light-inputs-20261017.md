# 决策总览第一批减负验收（2026-10-17）

## 范围

只修改笔记本测试服代码。主机 `10.255.2.7:8000` 的数据库、采集、配置和生产代码不部署、不迁移。

- 只读 GPT current/hourly 使用专用 `gpt_runtime.decision_store.load_decision_store`。
- 前端只读 GPT 总览从同一批 API 结果投影卡片输入，跳过整库复制和旧模型计算。
- 保留卡片尺寸、位置、连线、模型取值、来源、时间、拦截原因、沿用最后有效建议的行为。
- 生产／DS／无只读标记／file 模式保留旧分支；DS 模型及训练基线不变。

本批未改：初始 `/state`、下方历史图表、生产 worker、历史归档、独立补录组件、后续业务输入 DTO。

## 同一输入的完整结果对拍

命令（仅允许已配置的只读测试环境）：

```text
backend/.venv/Scripts/python.exe -X utf8 backend/scripts/profile_readonly_decisions.py --compare
```

使用同一个主机只读事务、同一个主机时刻，分别调用旧／新加载器；比较 current 和 hourly 的完整 JSON，包含数值、模型、上下文、来源、状态、时间及原因。查询统计不含共同的 BEGIN 和时钟读取。

| 指标 | 原加载器 | 轻量加载器 |
| --- | ---: | ---: |
| 加载器自身 SELECT（隔离测试） | 14 | 4 |
| current + hourly 查询数（实库） | 105 | 85 |
| 同次受控样本耗时 | 5.875 秒 | 3.906 秒 |
| 输出 JSON 字节数 | 267659 | 267659 |
| 完整输出差异 | — | 0 |

此为一次受控顺序采样，不是 P95 或长期稳定性结论；共享的进程缓存及现场负载可能影响耗时。查询削减已由 SQL 事件计数和禁止历史表扫描的测试验证。85 次查询仍包含原有 Inputs 回退与业务查询，未宣称消除了全部历史读取。

## 隔离回归

```text
python -X utf8 -m pytest tests/test_gpt_decision_store.py -q --tb=short
40 passed

python -X utf8 -m pytest tests/test_readonly_decision_runtime.py tests/test_decision_sources.py tests/test_hourly_quality_advice.py tests/test_training.py -q --tb=short
85 passed
```

共 125 项后端测试通过，包含 DS 原版 9 项。覆盖 expert8／expert9／旧 GPT／损坏与缺失模型、激活边界、未来 Setting／AutoState／模型保护、停机与缺数回退、完整小时结果、默认分支和 SQL 预算。

前端新 `verify_gpt_light_snapshot.js` 及既有 overview／realtime／hourly／heartbeat 等纯 VM 回归通过。新回归在 `App.store` getter 和旧计算方法抛异常时仍可构造全部 18 个节点，覆盖无数据、0／false、三组分总量、参考节点详情来源时间、沿用值、原几何及连线。

DS 基线 `backend/data/train_js.json` SHA256：

```text
aa09b7a4574ec19be5f840b68f3611394b0584f2ec435b0abd062416f8566bbd
```

## 真实浏览器与隔离检查

Chrome 打开 `127.0.0.1:8000/#decision`，确认只读测试横幅、18 个主图节点、EXPERT8、小时建议视觉中心、近 10 小时表、即时／累加切换及动画。实际禁用旧 `readFacade` 后主图仍可构造。截图：`output/playwright/readonly-light-batch1-20261017.png`（本地验收文件，不入库）。

笔记本服务只监听 `127.0.0.1:8000`；使用原启动脚本启动，mode 为 `readonly-test`。旧笔记本数据库保持 1637724160 字节，mtime 为 `2026-10-16T07:02:07.156Z`，未更新。

## 并发读取待核查

浏览器验收发现：部分首轮接口出现快速 OperationalError，且后续轮询偶有 QueuePool 3 秒等待超时；请求恢复后核心计算和表格正常。顺序 health／telemetry／manual 读取均 200 且约 0.1–0.2 秒，但不能据此宣称并发稳定。

当前正在核查该只读传输／并发边界，不能把 500 全部归因于缓存、网络或本次模型输入投影。最终排查结论另行补充。
