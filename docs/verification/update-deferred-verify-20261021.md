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

## 主机当前状态(2026-10-21 14:20 实测)

- 10.255.2.7:8000 health ok;采集 connected、30/30、样本持续推进(旧代码,待更新)。
- 主机仍有上次尝试留下的 `backups/source-update-20261021-*/dense_medium.db`(约 6.28 GB,已结构完整但未全检)。本次重跑会新建备份目录;旧目录可留给管理员择期清理(磁盘剩余约 44 GB,建议至少保留最近一份)。
- 10.255.2.8 已不可达(主机当前地址为 10.255.2.7)。

## 下一步(需用户在主机远程桌面操作)

1. 打开下载页 `http://10.255.2.6:8765/`,重新下载"本次项目更新包(保留主机数据库)"。
2. 全部解压到**新文件夹**,运行 `UpdateProject.cmd`。
3. 预计节奏:备份复制 ~8 分钟 → 结构校验(秒级)→ 停服换码重启 ~2—4 分钟 → 采集重连+新采样确认 → 延迟全检约 25—40 分钟(此阶段采集已在新代码下运行,不受影响)。
4. 窗口最后显示 `installed` 即成功;笔记本侧会通过回报通道持续观察。
