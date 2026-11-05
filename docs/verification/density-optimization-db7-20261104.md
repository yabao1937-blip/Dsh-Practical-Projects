# 新版近7天数据库重算与测试端交付（DB7）

本轮已从逐次数据库记录及其原始遥测引用恢复近7天输入，**10,068个真实采样时点全部评估**。测试端即时/小时主建议已接新版，趋势页改为新版，不再新旧并排；同时导出可独立查看的PNG、SVG和全量CSV。生产未部署，没有写控制、回填历史或训练模型。

测试地址：**http://127.0.0.1:8000/**。当前实际监听PID **59336**，启动器父PID15304，实例保留供查看。运行模式实测 `readonly-test`，源为真实主机10.255.2.7；新运行证据仅写DB7目录。

## 冻结窗口与完整性

**北京时间2026-10-28 10:04至11-04 10:04，结束不含**；UTC为10-28T02:04:00Z至11-04T02:04:00Z。窗口以主机分钟截止冻结，不混用滚动查询。

|全量结果|条数|
|---|---:|
|评估的真实采样|10,068|
|新版建议调整|868|
|保持当前设定|1,250|
|等待观察，保留当前设定|1,722|
|不可用、建议留空|6,228|
|明确给值（调整/保持/等待）|3,840|

“全量评估”不等于每点必须给建议。不可用包括：实测无效/不新鲜3,756、开停机状态不能确认284、缺当时可用的在线参考来源267、历史参考无值或上下文切换1,921。最后一类由历史missing 1,920和真实上下文切换1构成，不能把它解释成批量适配丢字段，也没有为补图降低质量要求。868个调整中，131为常规限幅，737为暂估谨慎限幅。

浏览器图保留**2,281个真实原点**：按极值、状态/原因/质量边界及真实缺口两侧保留，不插值制造点；统计仍以全量10,068为分母。存在7段真实采样缺口，最长375秒。网页按 `break_before`断线，静态导出也使用这7个经全量cadence验证的标记，不按下采样点间距猜断点。

## 数据恢复和独立核对

后端执行代理为唯一主动远端DB提取者。恢复使用DecisionCycleSample及 `$reading`/`$manual`、point-metadata目录解码、历史context定义和当时建议归档；没有用旧月动作快照的稀疏范围代替数据库范围。历史context保留target、model_definition、density_guide、input_timing及来源/定义。源码恢复路线及限制见 `integration/source-recovery-review.md`。

独立复核固定小时10-28 20–21北京时间：60样本、181个唯一原始TelemetryReading主键全部找到；**120/120个MD/MD_SET输入cell**的source/point/batch、definition、received_at和值与原始主键记录一致，逐次slot与重算slot一致。两路observed/received均不晚于采样时刻，无future标记。该小时52条MD=1.0不能作有效实测，另8条停机工况held/旧观测不能确认，均不可用；输入成功恢复与建议可用性已分开。

根线程另外对全量核对通过：窗口、唯一性、raw哈希、先验known-at不晚于采样、人工化验created_at可用时间、hold/wait等于真实历史设定、调整限幅/实测跟随/调后灰分覆盖、图点逐行来自全量、状态和质量边界两侧、7处缺口及有效实测DTO。脚本与结果为根目录 `root-check-replay.py` / `root-replay-check.json`。

## 重算口径：不使用未来数据，不模拟已经执行

- 每点使用**真实历史MD_SET和实测密度**。候选没有被递推成下一点的“实际设定”，也没有据假设新执行生成后续灰分。
- 灰分输入是该点当时known-at的生产小时/暂估版本。图下方现场结算灰分只是查询时的历史背景，不能反向作为候选先验。结算含计算/预测组分，非暂估也不等同独立正式总精煤化验。
- 刚到整点但在线依据尚未生成的首点，正确留空而不回填之后出现的建议。无已知来源、参考无值、输入不新鲜或工况不能确认时均保留原因。
- 调整上限仍为常规0.010、暂估0.005 g/cm³；最近动作尚无完整调后生产小时或实际密度未跟随时等待。单次跟随读数不代表稳定，等待小时也不代表已识别最佳物理滞后。

独立代表点手算（北京时间）：

|时点|当时历史设定/归档参考|新规则与结果|
|---|---|---|
|10-30 00:01:05|1.490 / 1.507，非暂估|`round(1.490 + min(0.010, 0.017), 3)` = **1.500**，与重算相同|
|10-30 01:02:05|1.490 / 1.499，暂估|`round(1.490 + min(0.005, 0.009), 3)` = **1.495**，与重算相同|
|10-28 10:04:05|历史设定1.480，灰分尚未完整覆盖最近动作之后|**等待观察1.480**，没有继续追调|

另已核对实测未跟随、灰分带内保持和无效实测留空案例。全部六例保留在 `integration/representative-hand-checks.json`。它们证明规则和输入时序，不证明未执行新版会改善灰分或产率。

## 页面和主建议只使用新版

测试端 `GET /api/v1/decision/current` 和 `/hourly` 实测均为 `density_policy=density-candidate-v1` / `density_version=database-trial-v1`。保存的实机探测快照中，A304当前设定1.520、新建议1.530、一步+0.010；`suggested_density`、主cells、guidance、execution_guidance与新规则结果一致。现场数值以后会随只读数据改变，不把该固定样本说成永恒当前值。

`GET /api/v1/decision/evidence/density-series?limit=168` 返回冻结7天数据，**只读本地series/manifest/raw，不重新批量查生产库**。主图不再并排旧参考线/旧列；保留人工设定作为实际对照。实测的无效1.0在DTO里value为null，原raw_value/source_status保留审计。完整有效实测尖峰也不删，默认实测关闭、用户可打开，默认可见设定/新版曲线据可见系列定轴。

API及只读保护实测退出0：runtime/current/hourly/series均HTTP200，本地写探测HTTP403。进一步只读核对已保存响应，确认主建议所有别名相同、固定窗与series一致、2,281个图点的实测值严格服从usable/null，不再请求网络。

## 独立图和复现文件

主输出位于 `output/density-optimization-db7-20261104/integration/`：

- `db7-new-density-trend.png` / `.svg`：全量新版建议与人工MD_SET直接同轴；**绿色为调整/保持，橙色点虚线为等待观察**；灰分独立panel，真实缺失断开。
- `db7-new-density-full.csv`：10,068行，含UTC/CST、真实设定、新下一步、状态/理由、有效及raw实测、当时灰分依据、known-at、context和gap标记。
- `db7-ash-production-background.csv`：生产时段的结算背景，与候选的当时灰分输入分开。
- `independent-one-hour-audit.json`、`representative-hand-checks.json`、`export-summary.json`、`saved-api-consistency-check.json`。
- `api-db7-*.json`、`api-db7-probe-summary.json`、`runtime-state.json`、`process-final.json`及各命令日志。

导出脚本 `backend/scripts/export_density_candidate_db7_20261104.py`，实际命令：

```powershell
backend/.venv/Scripts/python.exe backend/scripts/export_density_candidate_db7_20261104.py
backend/.venv/Scripts/python.exe backend/scripts/export_density_candidate_db7_20261104.py --probe-only
backend/.venv/Scripts/python.exe backend/scripts/export_density_candidate_db7_20261104.py --verify-saved-api
backend/.venv/Scripts/python.exe backend/scripts/export_density_candidate_db7_20261104.py --render-only
```

四个命令均已退出**0**。最后render-only只从既有CSV重渲染等待样式，没有重复取数或重算。绘图库复用旧任务隔离依赖，禁止写其bytecode，字体cache放新DB7目录。

## 验证、保护与交付状态

- 后端最终受影响组 **109 passed / exit0**，见 `backend/db7-final-tests.log`、`db7-final-test-command.json`；不与早期77或OPT-1的144累计。纯函数SHA保持174ebc…，本轮重算无训练/调参。
- 根线程全量独立核验通过，见 `root-replay-check.json` 和 `root-acceptance.md`。最终PNG目视通过：等待为独立橙色点线，调整/保持为绿、实际设定为蓝；窗口/灰分背景/缺口说明齐全。最终源码及产物固定在 `integration/candidate-final-hashes.json`。
- UI最终new-only桌面/390、即时/小时主建议、缓存失败路径及最后窄显示修正均通过，见 `ui/ui-handoff.json`、`browser-verification.json`、`cache-failure-verification.json`、`final-render-verification.json`、`overview-final-verification.json`。缓存测试是隔离浏览器中旧cache1.599/合成pending/真实离线请求失败场景，不冒称生产现场数据截图。默认密度轴紧凑对比新版与人工设定；实测按钮可完整打开所有有效尖峰，未删数据。主展示理由及总览共用helper不再使用旧版/候选措辞。根线程已亲看最终桌面/手机图；6个修改加1个保留基线共7项UI指纹、11项后端指纹已核对。
- 专用runner显式 `--output-dir` resolve后仅允许DB7目录，新runtime/probe/log没有覆盖OPT-1。原runner基线已按OPT-1旧指纹认证留档。
- 已核对OPT-1固定输出36文件、原月候选13项及57个月原始响应全部未变。活跃旧服务日志允许自然追加，不在固定hash内。
- 用户业务验收尚未记录；测试端保留运行，生产未激活新版。

最终图/画面：独立导出 `integration/db7-new-density-trend.png` / `.svg`；浏览器默认新版图 `ui/desktop-trend.png`、`ui/mobile-trend.png`，以及 `ui/density-instant-details.png` / `density-hourly-details.png`。所有本轮runtime、API、图和报告写DB7新目录；未覆盖原OPT-1/月分析固定输出。最终series SHA256为 `c64f3770b540245bfe338a67876fec7d0e689b1a3dee281e2451d88b59cf28b8`。
