# 决策总览非部署验收

2026-10-20，接续 10 月 17 日暂停的剩余验收。范围为笔记本测试服及临时合成库；主机未部署、未写入测试数据。本次保留四批已完成的布局与业务口径，完整回归、真实页面补录更新和 30 分钟持续稳定性验证均已通过。

## 修正的问题

1. 旧库缺少 `manual_measurements` 表时，启动升级仍尝试增加 `time_basis` 列。现在仅在表存在时升级该列和索引；原有旧数据保留、两条皮带可同时记录的测试通过。提交 `82a9626`。
2. 点位元数据变更测试仍用 `normalize()` 的原始结果与持久化结果整体比较，遗漏已加入的批次与展示记忆。改为先验证原采样字段，再保存完整持久化基线，确认元数据更新不改变它。提交 `82a9626`。
3. 外部主机补录后，小时表已经变化，小时建议仍等待正常 30 秒轮询。真实三页面测得 31.15–37.45 秒，未达验收目标。现在只读测试服在最新已发布小时的灰分、覆盖率或暂估状态变化时刷新小时建议；内容相同时不额外刷新，生产页面刷新频率不变。默认近小时只读查询允许有界冷回算完成，避免超过原 10 秒超时后进入 30 秒退避；显式历史查询及调用方取消信号保持原行为。提交 `803e54b`。

## 完整回归

- `backend/tests` 全部 **1,107 项通过，0 失败、0 错误、0 跳过**，耗时 **1548.01 秒**。包含 DS 原版九项对拍。唯一警告为现有 Starlette TestClient 对 httpx 的弃用提示。
- **32 组纯前端 VM 全部通过**，包含只读启动与写入拒绝、来源/目标、实时/小时建议、灰分历史、独立补录、导航及旧计算分支。
- 旧库升级、采样元数据、实际生产时间和手工补录相关 **62 项**先行组合回归通过。
- 10 月 17 日暂停的回归和 540 秒超限运行均不计为通过。本次重新完整执行并保存 JUnit；原 28 文件范围在本次运行合计 380 项、556.42 秒，不能据此反推当时停止的具体用例。测试目录明确为 `backend/tests`，不收集数据目录中的历史源码副本。

完整回归只在测试子进程设置 `OPENBLAS_NUM_THREADS=1`、`OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`，限制数值库线程竞争；未修改服务环境或模型。完整日志与 JUnit 为 `output/acceptance-20261020/full-backend.log`、`full-backend.xml`；前端逐组退出码为 `frontend-vm.json`。

```powershell
cd D:\dense-medium-density-control-system\backend
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
& .venv/Scripts/python.exe -X utf8 -m pytest tests -vv --tb=short --durations=30 --junitxml=../output/acceptance-20261020/full-backend.xml
```

## 真实页面补录更新

`verify_readonly_settlement_e2e.py` 创建独立临时 SQLite 主机、认证只读桥接、运行实际笔记本后端与三张 Chrome 页面。只有临时回环主机接收补录 POST；生产主机不接收测试补录。十小时共 **7,000 条**五秒采样，每小时 700 条、有效覆盖率 97.22%；EXPERT8 使用合成合法模型，未加载或训练 DS。浏览器通过正常应用轮询观察外部补录，没有强制刷新或模拟时钟。

| 场景 | 三页面完整更新耗时 | 结果 |
| --- | --- | --- |
| 最早一小时补录，十小时沿用段重算 | 11.57–11.71 秒 | 十行与小时建议同步 |
| 同小时更晚采样替换 | 8.80–10.09 秒 | 采用最新采样 |
| 后续小时出现真实采样 | 6.94–9.43 秒 | 后续沿用段改用该采样 |
| 更早时段晚补 | 4.94–9.54 秒 | 仅修改下一真实采样之前的沿用段 |

所有场景均在 30 秒内完成；三页面灰分修订一致，小时建议始终使用原有固定 MD_SET 基准。原始采样、冻结小时与基准等非补录表内容哈希不变；未创建笔记本回落数据库，桥接事务全部释放；浏览器 HTTP、请求与 JavaScript 错误均为零，测试服写请求为零。

```powershell
cd D:\dense-medium-density-control-system
& backend/.venv/Scripts/python.exe -X utf8 backend/scripts/verify_readonly_settlement_e2e.py --output output/acceptance-20261020/e2e-final --playwright-module C:/Users/25925/AppData/Local/npm-cache/_npx/31e32ef8478fbf80/node_modules/playwright
```

结果为 `output/acceptance-20261020/e2e-final/readonly-settlement-e2e.json`，截图为同目录 `readonly-settlement-e2e.png`。该截图使用合成数据；采集值超过有效期后允许不可用，历史小时建议依然有效。运行应避开整点前两分钟，以免四个连续场景跨入下一发布窗口。

## 持续稳定性

最终代码三张独立 Chrome 页面从北京时间 **15:49:46 至 16:19:52** 持续运行 **1,806 秒**，跨过 16:00 整点，结果 `passed: true`。共 **1,947 次浏览器接口请求**，另有 **400 次性能探针请求**；153 次页面状态检查均保持 18 节点、10 行小时表、后端在线，JavaScript/HTTP/请求/状态错误、写入尝试和整库读取均为 **0**。观察到的页面 JS 堆约 4.0–6.2 MB，不能据此推断长期无泄漏。

| 接口 | 确认缓存复用样本数 | 热读 P95 | 含冷计算的整体探针 P95 |
| --- | --- | --- | --- |
| 采集与补录快照 | 48 | 74.33 ms | 573.37 ms |
| 即时决策 | 50 | 122.03 ms | 6149.60 ms |
| 小时建议 | 50 | 224.75 ms | 234.76 ms |
| 小时灰分表 | 50 | 27.82 ms | 443.61 ms |

四个接口的缓存热读 P95 均低于 500 ms。探针先等待一次读取完成，再紧接着读取并比较生成时间，只有确认缓存复用的第二次读取才进入热读分组。即时决策的冷计算仍约数秒，未宣称所有请求均达到 500 ms。结果为 `output/acceptance-20261020/readonly-soak-accepted.json`、`readonly-soak-accepted.log`；中途修改前的两次运行不计为最终验收。

最后重新拉起笔记本服务，`runtime-mode`、`health/live`、`health/ready` 均返回 200；随后三页面 60 秒启动检查通过，68 次浏览器接口请求、零错误，见 `restarted-smoke.json`。独立只读复核未发现阻断项，报告数字与原始结果一致。本轮源码与记录仅在本地提交，未推送或部署。

```powershell
$env:DMCS_PLAYWRIGHT_MODULE='C:/Users/25925/AppData/Local/npm-cache/_npx/31e32ef8478fbf80/node_modules/playwright'
& D:/KF/nodejs/node.exe backend/scripts/verify_readonly_soak.js --seconds=1800 --report=output/acceptance-20261020/readonly-soak-accepted.json
```

## 数据与部署边界

- 服务为笔记本 `127.0.0.1:8000`，`readonly-test`，只读主机 `10.255.2.7:8000`。
- 笔记本旧库 `backend/data/dense_medium.db` 仍为 1,637,724,160 字节，修改时间 `2026-10-16T07:02:07.156Z`。
- DS 基线 `backend/data/train_js.json` SHA256 仍为 `aa09b7a4574ec19be5f840b68f3611394b0584f2ec435b0abd062416f8566bbd`，未改 DS 训练、特征、聚合或保存模型。
- 生产端部署、部署后的真实写入验收和更长时间现场观察留待后续；本次合成端到端验证与有限持续运行不替代这些工作。原有压缩归档和备份机制保留，本次未在主机新增备份任务。

先前四批实现记录见 [第四批验收](decision-overview-core-workspace-20261017.md) 与 [第三批结算验收](decision-overview-local-settlement-20261017.md)。
