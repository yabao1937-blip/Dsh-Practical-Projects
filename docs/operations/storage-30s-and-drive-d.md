# 五秒实时显示、三十秒历史存储与 D 盘迁移

## 本次变化

KEPServer 仍每 5 秒读取，实时卡片和即时计算使用有界运行缓存。正常原始历史和逐次决策每 30 秒保存一次，正常历史批次数约为原来的六分之一。质量变化会额外保留事件；手工补录、配置变更和小时结算不受“必须等满 30 秒”的限制。

缓存只保留最新状态、短时环形读数及少量显示记忆，不成为永久数据库，也不能用于伪造历史引用。实时模型的液位仍只提前 90 秒一次。历史回算只用已落库的真实记录。过去的 5 秒历史全部保留，不重新生成 DS 数据或模型。

新记录标明自身的采样周期；小时覆盖率、灰分产量权重和密度统计识别 5 秒与 30 秒混合数据。缺失一个 30 秒周期不能只算缺 5 秒，也不能跨缺口补齐。正常历史不再逐条保留所有 5 秒短暂波动。

主机旧配置明确写有 `history_interval_seconds: 5`，因此只升级代码默认值不足。本次标准更新包显式携带 `collector_history_interval_seconds: 30`：备份通过、所属采集进程停止后才修改该字段；保持 `poll_seconds: 5`、点位、认证、模型和其他配置，失败恢复原配置字节及原代码。

## C 盘与 D 盘的安排

程序、Python 和现有启动入口仍留在：

`C:\Users\XMC\DshDensityControl`（本次现场核实的安装路径）

将两个主要增长目录迁为：

| 原路径（保留目录联接） | 实际存储位置 |
| --- | --- |
| `backend\data` | `D:\DshDensityControlData\data` |
| `backups` | `D:\DshDensityControlData\backups` |

数据库及 WAL/SHM、待补写 spool、数据目录内的缓存和已有备份整体复制。这样不需要改数据库环境变量、快捷方式或虚拟环境，后续数据库增长及更新备份也使用 D 盘。D 盘须为本机固定 NTFS 磁盘，并有足够空间容纳源数据和备份。

## 主机操作顺序

1. 从笔记本的离线下载页下载 **D 盘数据迁移工具**，全部解压到新文件夹。
2. 关闭正在运行的更新窗口及手工看门狗窗口，运行 `MoveStorageToD.cmd`。迁移期间不要同时启动服务或其他更新脚本。
3. 工具只停止本安装的进程，完成 WAL 检查点、文件复制与逐文件 SHA256 比对，再创建目录联接并检查服务就绪和新采样。复制、哈希期间会暂停采集；数据库和备份较大时耗时也较长，窗口会输出进度。
4. **迁移成功后，原 C 盘副本仍保留，尚未释放这些空间。** 在迁移工具解压目录运行以下命令预览待清理路径：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\FinalizeStorageMove.ps1
   ```

5. 核对预览确为本次生成的两份 `*.before-D-*` 原 C 盘副本后，运行以下命令释放空间：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\FinalizeStorageMove.ps1 -ConfirmRemoval
   ```

   工具会再次核对 D 盘实际路径、服务就绪和新采样，只清理记录中两份精确的旧 C 盘普通目录，保留正在使用的 D 盘数据及历史备份。

6. 再下载 **本次项目更新包（保留主机数据库）**，全部解压到另一个新文件夹，运行 `UpdateProject.cmd`。成功后刷新浏览器。

不要手工只搬 `.db` 文件或删除联接目标。迁移开始前会检查空间及路径；启动前失败可以恢复原 C 路径，启动后失败先保全 D 盘新增数据再恢复。若报告 `recovery_required`、遭强杀或断电，保留两边数据和 `D:\DshDensityControlData\storage-migration.json`，按实际阶段处理，不重新覆盖运行。

## 验收状态与证据

本次功能先在隔离临时数据库及 Windows 临时目录验证，不把本地测试通过等同于主机已部署。主机执行前后还需核对真实路径、配置、健康状态、实时采样推进与 30 秒历史批次。

2026-10-23 现场已完成 D 盘迁移和旧 C 盘副本清理。清理脚本先确认新采集进程已有两次推进的提交，再移除 `backend\data.before-D-20261023-020305-766032`（7077 MiB）及 `backups.before-D-20261023-020305-766032`（31607 MiB），合计约 37.8 GiB；D 盘生产数据及历史备份保留。随后首次 30 秒入库更新在停止采集进程时超时并回退，主机恢复在线，不能将此次迁移成功等同于 30 秒入库已生效。

停止辅助脚本改为先捕获本安装各采集进程的原生句柄，再核对完整脚本路径及进程创建时间，向全部已核实的进程发出停止信号后，按同一个 60 秒上限等待真实退出；不再以 CIM 记录是否消失判断执行是否结束。Windows 虚拟环境启动器和解释器可能同时匹配，实际进程退出后继续可见的 CIM 记录由隔离回归模拟；现场底层延迟原因尚不能仅凭日志确定。报错会列出仍未退出的 PID，且等待上限提示与实现一致。

- 采集/缓存专项及原采集回归：`test_telemetry_realtime.py`、`test_collector_storage.py`、`test_telemetry.py`。
- 主机缓存只读传输及测试服回归：`output/storage-30s-20261023/transport.xml`、`live-final.xml`。
- GPT 输入、液位时间口径、历史保留及 DS 原版：`output/storage-30s-20261023/core.xml`。
- 混合周期、覆盖率、小时回算和引用：`output/cadence-archive-final.xml` 及最终补丁复跑 `output/cadence-final-patch.xml`；分批用例结果，不能视为单轮完整运行。
- 配置更新及失败恢复：`output/storage-policy-update-tests.xml`。
- 迁移、晚失败新增数据回滚、清理范围及 Windows 进程归属：`output/storage-migration-20261023/tests.xml`。
- 采集进程退出、虚拟环境启动器/子进程、保留句柄、滞留 CIM 记录、PID 身份及无关进程保护：`test_stop_owned_collector.py`、`test_stop_owned_collector_handles.py`。

停止辅助脚本修复后的独立验收：上述两组进程测试连同项目更新、打包及存储迁移测试共 99 项通过（`output/storage-30s-20261023/stop-update-retry.xml`）。此结果仅证明隔离验证通过，30 秒策略仍需主机重试成功后核对实际批次。
