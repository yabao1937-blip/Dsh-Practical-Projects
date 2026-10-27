# 近七天小时建议密度回补（advice-backfill-v1）· 2026-10-27

## 背景与目标

密度趋势对比页的小时建议密度来自 `gpt_hourly_density_advice` 轻量归档，而该归档 2026-10-27
才随部署生效（当天 08:00 起每小时归档），此前的历史小时没有建议存档，图表近 7 天档位缺少
小时建议密度曲线。用户 2026-10-27 指令：按系统当前算法、利用已存储的其他因素（小时设定密度
锚点、延迟结算灰分、逐次证据、当时来源/模型/参数），回算近 7 天缺失的小时建议密度，使图表
有数据。

> 此前「历史留空、不回算」的口径由本指令取代；真实缺口（灰分覆盖不足等被算法闸住的小时）
> 仍如实留空，不用旧统计值或当前参数冒充。

## 实现方式

- 新增 `backend/app/decision_hourly/advice_backfill.py`（ORIGIN=`advice-backfill-v1`）：
  - 逐小时取 `GptHourlyDensityAnchor` 锚点的 `captured_at` 作为回放时刻；
  - `shared_snapshot(when=锚点捕获时刻)` 还原当时的来源、模型、设置与已录入化验
    （`read_runtime` 的新鲜度围栏天然排除当前实时缓存，无今值泄漏）；
  - 完全复刻 `service.read_hour` 的调用序列，仅做一处替换：兼容性检查的参照定义用
    **该小时锚点自带的存储 context**（系统当时保存的定义），而不是用今天的代码重算的定义
    ——与在线检查当时所比较的对象一致，消除代码演进造成的假「配置变化」阻塞；
  - 灰分结算用 `defer_missing=False` 就地重算（只用 `created_at ≤ 当时` 的化验，晚补化验
    不回溯进当时的建议）；
  - 写入走 `BEGIN IMMEDIATE` 独立事务、逐行提交，只增不改：已归档小时（含实时归档）一律
    跳过；写入行带 `origin=advice-backfill-v1` 标记；`REMOTE_READONLY` 只读端拒绝写入、
    仅允许 `--dry-run`。
- 主机一次性工具包 `backend/scripts/host_advice_backfill/`（`BackfillHourlyAdvice.cmd` 纯
  ASCII + `backfill_host.py` 引导 + 与仓库逐字节一致的 `advice_backfill.py`），打包为
  `outputs/offline-win10-x64/extras/BackfillHourlyAdvice.zip`（4,324 B），门户
  `offline_transfer.py` 新增 `advice-backfill.zip` 路由与「小时建议密度补档」栏目并已重启
  生效。工具不停服务、不改主机代码，仅向 `gpt_hourly_density_advice` 追加带 origin 的行。

## 验证

1. **pytest**（隔离临时库）：新增 `backend/tests/test_hourly_advice_backfill.py` 6 项全过
   （回放值/origin/A304 基准、已有归档不重写、幂等、缺上一小时统计跳过、dry-run 零写入、
   只读端拒绝写入）；相关回归 `test_hourly_advice_archive/test_density_trend/
   test_hourly_quality_advice/test_hourly_setpoint_anchor` 70 项、打包相关 141 项全部通过。
2. **主机真实数据 dry-run**（经授权只读桥接，只读不写）：近 7 天窗口 158 个有锚点小时，
   8 个已有实时归档，150 个回放、0 失败；**87 个小时得出有效建议密度 1.500–1.559 g/cm³**
   （10-20:2、10-21:11、10-22:11、10-23:14、10-24:15、10-25:16、10-26:18），其余 63 小时
   被现行算法闸住（延迟灰分小时无有效「灰分-产量」配对或覆盖 ≤80%，全部 blockers=['ash']，
   无 configuration 阻塞），按口径留空。
   - 首轮 dry-run 曾出现 78 小时缺失，其中 10-20~10-22 大量为假「配置变化」；根因是用今天
     代码重算的定义与当时存储的 context 结构不一致，换用锚点自带 context 后消除
     （如 2026-10-21 00:00 本地小时恢复为 1.532 g/cm³）。
   - 桥接通道自身的限制（readonly_result_limit/readonly_query_timeout）在个别小时会中断
     只读回放；主机本地执行无此限制。
3. **门户**：`http://10.255.2.6:8765/<token>/` 首页出现补档栏目，`advice-backfill.zip`
   下载 200（4,324 B），包内模块 sha256 与仓库一致（777A7E98…3E374）。
4. **前端呈现**：趋势页建议曲线按数值点绘制、空值留断点；逐小时一点落在锚点捕获时刻
   （整点后数秒），相邻小时自动连线，`basis.channels=['A304']` 全部可绘（主机近 7 天
   174 个锚点全部为 A304 单通道）；同小时较晚的调密记录按「当时已生成建议」正确比对差值。

## 未覆盖项

- 主机上的正式写入与图表最终效果：工具包需由用户在主机下载运行（见交付说明），本次会话
  无主机写通道（只读桥接按设计不可写）。主机运行后可用
  `GET /api/v1/decision/evidence/density-trend` 的 `suggestions` 数量与
  `gpt_hourly_density_advice` 行数核对，页面 Ctrl+F5 查看曲线。
- 回补是一锤子定档：若事后为缺失小时晚补化验，按「已冻结建议不回溯重算」口径不更新已回补行。
