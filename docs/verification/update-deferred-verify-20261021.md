# 更新器大库校验修复与 2026-10-21 主机更新失败处置

## 背景与时间线(全部时间为主机/本机当地时间 +08:00)

- 上午:主机 10:13—10:15 出现 `OperationalError` 与 0/30(已另行修复,见 `kepserver-storage-contention-20261021.md`)。
- 12:22 codex 构建更新包(revision 28ba5d7,473 KB)发布到下载页 `http://10.255.2.6:8765/`。
- 12:47 用户在主机远程桌面运行 `UpdateProject.cmd`。
- 12:56—13:04 备份复制 6.28 GB(1,538,018 页,用时 493s,采集仍运行)。
- 13:04—13:32 备份完整性校验(`PRAGMA quick_check`)运行约 28 分钟后触发 20 分钟预算,脚本**安全中止**:`failed: Backup integrity check timed out; source files and running service were not changed.`。主机源文件与服务未被改动(已核实 health uptime 未重置)。
- **副作用**:校验阶段紧循环扫描打满磁盘 I/O,旧采集代码(写库与 OPC 会话耦合,15s 写超时)再次掉线:`state=retrying/offline, error_code=OperationalError`,最后入库样本 13:12:46。
- 13:37 经笔记本用页面下发 token 调 `POST /api/v1/telemetry/reconnect`,采集恢复 `connected` 30/30,13:38 起新样本持续入库。断档 13:12:46—13:38:21(约 25.6 分钟,与上午故障同因同型)。

## 失败定性

直接重试必然再败:6.25 GiB 库在这台 4 GB 内存、慢速磁盘的主机上,`quick_check` 需 >28 分钟,而校验预算固定 1200s。更严重的是,任何在**服务更新前**对备份做全量扫描的方案都会把还在跑旧代码的采集饿死——校验阶段没有节流,随机读打满磁盘后,采集的数据库写入在 15s 内得不到满足。

## 修复:按快照大小分级校验

`backend/scripts/windows_offline/apply_update.py`:

- `structure_fingerprint(db)`:页数 + 空闲页数 + sqlite_master 全量 SHA256。复制完成后立即从**源连接的固定快照**读取,再从备份连接读取一次,两者必须一致(秒级,顺序读)。
- `backup_database` 返回校验级别:
  - 快照 ≤1 GiB:原路径不变(复制后立刻 `quick_check`,1200s 预算)→ `content_check='full'`。
  - 快照 >1 GiB:只做结构指纹比对 → `content_check='deferred'`;**改动任何服务之前不再有全量扫描**。
- `apply()` 在更新后服务健康、采集重连并确认新采样推进、前端加载校验全部通过之后,才对备份执行延迟全量 `quick_check`(预算 = 300s + 6e-7 s/字节 ≈ 每 GiB 600s;256 MiB 页缓存)。此时磁盘上跑的已是新采集代码(spool 落盘、忙锁退避),扫描不再威胁生产采样。
- 延迟全检超预算或报损坏:**不回滚**(此时新代码已验收,生产库未被更新器碰过,快照保留供人工核验),结果如实写入主机 `source-update.json` 的 `snapshot_verification` / `snapshot_content_check` 字段。
- 结构指纹不一致(复制损坏/位翻转):更新中止,主机零改动,与原 `quick_check` 前置校验的保证一致。

## 验证

- `tests/test_project_update.py` 40 项通过(37 项原有 + 3 项新增):
  - 大快照走结构校验、延迟全检在新服务上线后运行且通过被记录;
  - 延迟全检失败不回滚、不掩盖安装事实;
  - 结构指纹不一致在任何服务改动前中止。
- `tests/test_package_project_update.py` 3 项通过。
- 更新包重建:`package_project_update.py` 带 codex 同款 accepted-baseline 参数,另接受 28ba5d7 自身(上次失败未动主机源码,源哈希未变)。产出 485,980 字节 / 98 文件,revision 仍为 28ba5d7;下载页实际下发字节与磁盘包 sha256 一致(`8f3c2da7…`),包内 `apply_update.py` 与本仓库工作区一致(含延迟校验逻辑)。
- 全量回归:`pytest tests` **1194 项全部通过**(25:32),见 `output/kepserver-20261021/deployment/full-suite-hermes.xml`。

注意:不带参数的 `pytest -q` 会递归收集 `backend/data/baseline-evidence-*/backend/tests/` 里的历史对拍副本,产生同名模块冲突;跑全量请显式 `pytest tests`。

## 第二次尝试(14:42—14:55)与停服脚本修复

用户 14:42 用新包重跑。备份复制 413s 完成(1,541,769 页,6023 MiB),**结构校验通过,更新前不再有全量扫描**——分级校验按设计工作。随后失败点后移:`StopOwnedWeb.ps1` 停 Web 服务时 120s 超时(紧接 6GiB 备份、页面压力峰值,脚本内**无过滤的 `Get-CimInstance Win32_Process` 全进程枚举**在小内存主机上被换页拖死)。回滚路径完整执行:旧源码恢复、Web 重启、采集以新 owner 重连并推进采样(14:53:35),生产无损。

修复(8083e15):`StopOwnedWeb.ps1` 枚举改为 `-Filter "Name='python.exe' OR Name='pythonw.exe'"`(与 StopOwnedCollector 一致,语义不变、开销降几个量级);新增 `tests/test_stop_owned_web.py` 4 项(进程名过滤断言、他装/无关进程保留、PID 复用防护、未识别安装拒绝),更新器相关 52 项全过。第三次包(486,418 B,sha 28eff2c5,revision 8083e15)已上下载页并核验一致。

## 第三次尝试:安装成功(15:13—16:33)

15:13 用户重跑(用户主机窗口 15:13 启动)。15:21 备份完成(6033 MiB,结构校验通过);15:22:47—15:23:51 停服+租约等待(StopOwnedWeb 修复生效,未超时);15:23:54 服务重启;**15:24:15 新采集代码上线**(新 owner、两次新采样确认);15:24:17 起 4095s 预算的延迟全检;16:33:25 全检达预算按设计停止(快照保留待人工核验),**16:33:26 回报 `installed`,healthy=true**。

### 验收实测(16:35—16:41)

- health ok;collector connected、错误码 None;A301 点位压力窗口(15:24—16:33 共 69 分钟,822 条样本)**最大缺口 16.8 秒**(仅 2 个 >12s 缺口)——同为磁盘高压,上午旧代码断档 193s+页面 0/30,新代码采样近乎连续。spool 补写机制实战兑现。
- 延迟全检期间(15:24—16:33)页面重查询(telemetry/latest、decision/*)出现约 40 分钟 HTTP 500(30s 超时)——扫描无节流打满磁盘所致;轻接口(health/live、settings)全程正常。**采样不受影响**。已于 973111a 修复:全检进度回调每秒 sleep 50ms,下次更新起扫描期间页面不再瘫痪。
- 小时建议接口如实拦截:15:00—16:00 覆盖率 75.6% < 80% 门槛(更新窗口所致),16:00—17:00 建议显示 `missing/ash`,属正确业务保护,后续整点自动恢复。
- 遗留:压滤机 A416/PLATE_CNT 自 15:59 起 OPC 坏质量(status_code 0x80000000),扫描结束后 8 分钟仍未恢复,其余 29 点正常——判定为现场侧(压滤机柜/KEP 通道)问题,非软件缺陷,待现场核查。

## 更新器后续改进承诺(未实施)

大库校验的更优路径(讨论定案,下次迭代):① backup API 逐页拷贝时顺手对每页算哈希,拷完对备份顺序读+哈希比对——字节级保真、耗时≈一次顺序读(~5 分钟),取代 quick_check 解码扫描;② 源库健康改为每日凌晨低峰定时巡检(带节流),不占更新路径;③ 增量备份(对上次已验证备份做页级 diff)。

## 主机当前状态(2026-10-21 16:41 实测)

- **10.255.2.7:8000 已运行新代码(revision 8083e15 更新,源为 28ba5d7)**:health ok、collector connected、29/30 可用(压滤机点位现场侧坏质量)、样本持续推进。
- 页面如显示旧样式,主机 Chrome 按 Ctrl+F5 强刷。
- 主机 backups/ 下现有三份 source-update-*(两次失败尝试的 6.28GB+6.03GB 快照与本次成功安装的 6.03GB 快照),磁盘约 44GB 剩余——建议现场择期只保留最新一份。
- 10.255.2.8 已不可达(主机当前地址为 10.255.2.7)。

## 下一步

更新已于第三次尝试成功安装,原「重跑 UpdateProject.cmd」指引作废。剩余事项:
- 现场:核查压滤机 A416/PLATE_CNT 坏质量点位(15:59 起);择期清理主机 backups/ 旧快照。
- 仓库:更新器「页哈希比对 + 错峰巡检 + 增量备份」改进(见上文承诺节)。
- 观察:新采集代码长期运行、整点行为与补录路径(kepserver-storage-contention-20261021.md 的部署后观察项)。
