# KEPServer 接入与运行维护

本轮提供 OPC UA 只读采集基础。2026-10-06 已按在线仪表清单接入正式 KEPServer，54 个标签精确匹配并开始持续入库，详见 [现场接入记录](KEPServer现场接入记录-20261006.md)。以前的 `D:\kepTest` 用于核对已测试的连接条件，采集器独立实现，不复制自动降级安全策略或输出认证信息的调试流程。

首次现场目标是验证少量点位的正确读取和故障表现。设备值目前只显示在“数据采集与补录”的 KEPServer 面板，没有自动进入原有仪表输入、灰分模型或密度建议。

## 两个进程与数据目录

| 项目 | 入口或位置 | 用途 |
|---|---|---|
| 网页后端 | `backend/run.py` 或 `uvicorn app.main:app` | 托管前端与业务/采集查询 API |
| 采集进程 | `backend/collector.py` | OPC UA 读取、批次缓存、入库与状态维护 |
| 本地配置 | `backend/config/kepserver.local.json` | 服务器和点表；已忽略 Git |
| 证书和私钥 | `backend/config/certs/` | 加密连接；已忽略 Git，仍需设置操作系统访问权限 |
| 批次缓存 | `backend/data/telemetry-spool/<source_id>/` | 已读取但尚未确认入库的批次；已忽略 Git |
| 数据库 | 默认 `backend/data/dense_medium.db` | 原有业务表和三张新增采集表 |
| 点表核对与首次入库记录 | `backend/data/telemetry-setup/` | 原始 NodeId、工作簿校验值、现场核对结果；忽略 Git |
| 后台进程与日志 | `backend/data/telemetry-runtime/` | 来源对应 PID、启动配置路径及日志；忽略 Git |

两个进程必须使用同一个 `DMCS_DATABASE_URL`。采集不会随 HTTP 服务自动启动；每个来源同时只运行一个采集进程。来源租约会拒绝重复启动，并阻止失去所有权的旧进程继续入库。

## 先确认现场信息

向现场维护人员核对服务器 IP、OPC UA 端口、可用端点、安全策略和访问权限。网线连通只说明网络链路存在，还需确认 IP 网段、路由和防火墙端口。不要为了连接测试开放不必要的端口。

每个点位至少确认：NodeId、名称、对应系统/皮带、值类型、单位、缩放比例、偏移、合理量程、允许延迟。示例的 `Channel.Device.Density` 是占位符，不能当成现场真实点表。

确认 KEPServer 是否支持本实现的 `Basic256Sha256 / SignAndEncrypt`，固定服务器证书，并在服务器侧信任采集客户端证书；客户端证书的 Application URI 必须等于配置的 `application_uri`。本实现检查证书有效期和 Application URI，并使用固定服务器证书；证书用途、撤销、完整证书链管理和服务器主机名校验尚未建成统一 PKI 流程。

建议使用只读账号并由部署环境提供凭据。JSON 内仅填写 `username_env`、`password_env` 的环境变量名称，不填写密码。代码没有自动退回不加密连接的逻辑。

如果已确认旧服务器只能匿名、无安全策略访问，配置必须同时显式设置 `security: "None"` 和 `allow_insecure: true`，并取消账号环境变量。此模式不加密数据，只能在确认过的受控测试环境中由维护人员选择；不能作为失败时的自动后备连接。

## 安装和配置

以下命令在仓库的 `backend` 目录运行，使用 Python 3.11。已有虚拟环境可直接使用。

```powershell
cd D:\dense-medium-density-control-system\backend
.\.venv\Scripts\python.exe -m pip install -r requirements-kepserver.lock.txt
.\.venv\Scripts\python.exe -m pip check
if (-not (Test-Path -LiteralPath config\kepserver.local.json)) {
    Copy-Item -LiteralPath config\kepserver.example.json -Destination config\kepserver.local.json
}
```

目标本地配置已存在时，直接编辑原配置，避免覆盖现场点表。锁定采集依赖包含原有锁文件；`requirements-kepserver.txt` 仅声明可选采集依赖，正式部署使用 `.lock.txt` 组合。

先保持 `enabled: false`，填好地址、点表和证书路径。证书相对路径以配置文件所在目录为基准。`scale` 和 `offset` 的换算为 `value = raw_value * scale + offset`，量程检查针对换算结果。

```powershell
.\.venv\Scripts\python.exe collector.py --config config\kepserver.local.json --validate
```

`--validate` 只检查配置，不连接设备、不写数据库。`enabled: false` 时不验证证书文件是否存在；准备连接前改为 `true` 再校验，才能检查启用条件和证书文件。此命令不证明服务器可连接，也不代替证书握手验证。

## 数据库升级边界

先备份并停止相关写入进程，在数据库副本上验证，再升级现场库。备份 SQLite 要使用一致快照方法；只复制 `.db` 可能遗漏 WAL 中的数据。现有强制业务恢复已使用 SQLite 在线备份 API，备份失败返回 503 并取消覆盖。

**新建的空数据库**在首次启动服务之前执行：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

当前 head 为 `c3a610060001`。0002 已改为 SQLite 可运行的批量表变更；0003 增加三张采集表；0004 增加六列在线仪表视图。历史现场库没有迁移基线时，继续按下述兼容规则处理，不盲目 stamp 或全链升级。

**已有且迁移版本为 `b7e2c4d9a001` 的库**，0003 能接纳新 HTTP/collector 自动创建的兼容采集表。迁移先核对列、主键、批次唯一约束与索引，不匹配就停止；不会以盲目 stamp 掩盖结构差异。已验证旧业务行和已有采集行保留。

**没有 `alembic_version`，或迁移状态落后但表已存在的历史库**，不能直接照抄 `upgrade head` 或 `stamp head`。旧版一直存在 `create_all` 和兼容升级流程，这轮继续保留；先在副本检查表结构与版本，再建立迁移基线。0003 的兼容检查不等于解决所有旧版 0001/0002 基线问题。

没有对现场数据库运行迁移。Alembic 降级会删除三张采集表及其历史，必须先另行备份；正常回滚优先回滚代码并保留数据，不执行降级清表。

## 单批验证与持续运行

配置设为启用且 `--validate` 通过后，先读取一批：

```powershell
.\.venv\Scripts\python.exe collector.py --config config\kepserver.local.json --once
```

`--once` 会连接配置指定的服务器并写入采集表，成功后退出。首次联调使用少量点位，与 KEPServer 客户端逐点比较原值、换算值、质量和时间。单次读取退出后来源状态会变成“已停止”，网页读数仍保留，但不会标成持续采集中。

确认后分别运行网页服务和持续采集：

```powershell
.\.venv\Scripts\python.exe run.py
```

```powershell
.\.venv\Scripts\python.exe collector.py --config config\kepserver.local.json
```

打开 `http://127.0.0.1:8000/` 的“数据采集与补录”页。后端在线与采集在线是不同状态；后端数据库正常时，也可能尚未配置采集、采集已停止或证书连接失败。

现在是前台进程运行方式，尚未安装 Windows 服务或计划任务。正式长期运行前要配置进程托管、日志轮转和权限，两个进程分别监控。现有 `watchdog_server.ps1` 改用 `/health/live` 判断网页进程，不因数据库短时故障反复重启；数据库故障应通过 `/health/ready` 另行报警。

需要在本机后台启动采集时，在 `backend` 目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\start_collector.py --config config\kepserver.local.json
```

此入口先检查启用配置和来源租约，已有采集进程时不会重复启动；输出 PID 与日志路径。采集作为独立后台进程运行，关闭启动终端和网页不会停止；没有安装开机自动运行的 Windows 服务或计划任务。启动后仍需检查采集 API 的 `connected` 状态及读数时间是否前进。

## 质量与时间口径

| 字段或规则 | 含义 |
|---|---|
| `raw_value` / `value` | 协议原值 / 换算值；0 和 false 是有效数据 |
| `quality`、`status_code` | OPC UA 的 Good、Uncertain、Bad 与原始状态码 |
| `source_at` | 源时间，可能表示最后值变更时间 |
| `server_at` | OPC UA 服务器返回的服务器时间 |
| `received_at` | 本系统收到对应读取分块的时间；不同分块不会伪装成同时收到 |
| `available`、`status` | 根据质量、类型、量程、来源在线和时效综合判断的可用性及原因 |

存储统一使用带时区的 UTC 时间，界面按浏览器所在电脑时区显示，并在时间单元格提示中保留原始时间。OPC UA 默认/空时间戳显示为缺失，不填造的时间。

默认每 5 秒轮询，点位接收超时默认为 30 秒。`SourceTimestamp` 可能在值不变时保持不变，所以默认不能仅凭源时间旧就认定设备离线；只有现场确认该点源时间口径后，才填写 `max_source_age_seconds`。源时间明显晚于接收时间会标为时钟异常。

Bad、Uncertain、类型不匹配、非有限值、量程外值、过期或来源离线都不标可用。查询失败会保留最后读数供核对，同时明确显示不可用。所有这些读数目前都不自动驱动原有业务计算。

## 历史和故障保证

2026-10-14 更新：默认每 5 秒读取一批并保存全部点位历史，最新值每批更新。原始采集历史永久保留，已取消定期删除；`retention_days` 保留作旧配置兼容字段，统一转换为 `0`（不设期限），旧配置中的 `7` 等天数不会重新启用清理。

逐次决策超过 7 天后仍可无损压缩至同一 SQLite 的 `gpt_decision_evidence_archives` 表；压缩内容校验成功后才移除对应热表记录，查询会自动解压读取，归档和引用的原始采集记录均永久保留。小时汇总、化验补录、反馈记录同样没有按时间自动删除的任务。永久保留从本次更新起生效，不会自动恢复此前已被旧规则删除的数据。

整库覆盖前生成的恢复备份也永久保留，新备份不再淘汰超过最近 20 份的旧文件。运行日志轮转、已成功落库的采集缓存确认删除继续执行，这两项不删除数据库中的历史记录。

磁盘缓存的保证是：**完整读取且已落盘的批次**在数据库提交成功前保留，重启可重放，重复批次不会重复入库；较旧批次或已变化点位定义的批次不能回退当前读数。

数据库故障时，会保留已落盘批次并暂停新的采样，先恢复入库再继续读取。网络断线期间无法凭客户端补出未读取的 PLC 数据；需要服务器历史能力和专门的历史补采设计。缓存也不覆盖断电发生在落盘之前的读取窗口。

缓存容量默认每来源 50 MiB，损坏、容量超限或缓存包含无法识别的点位时显式故障停止，文件不会被静默删除。已变更但仍可识别的旧点位定义可保留在历史中，不能回退当前值。修复前先保留问题文件，核对点表与磁盘状态；不要通过清空缓存掩盖丢失情况。

| 现象 | 核对顺序 |
|---|---|
| 后端离线 | 进程、端口、HTTP 日志、`/health/live` |
| 数据库未就绪 | `/health/ready`、磁盘、文件权限、必要表与迁移状态 |
| 未启动 / 待配置 | 是否运行 collector、是否 enabled、两个进程数据库是否一致 |
| 断线重连 | 服务器网络/端口、OPC UA 会话；重试间隔有上限和抖动 |
| 配置或连接异常 | 日志异常类型、证书有效期/URI/信任、安全策略、账号、点表；修复后重启采集 |
| 心跳中断 | 采集进程是否意外退出；租约到期后网页不会继续显示在线 |
| 量程或类型错误 | 对照真实值、单位、缩放和量程，禁止先放宽校验掩盖接线/点位问题 |

HTTP 响应附带 `X-Request-ID`，日志用该编号关联请求。日志没有请求体、查询参数或认证头；未知异常响应不会回显内部连接信息。

## 接口和复现验证

查询最新状态：`GET /api/v1/telemetry/latest`，可加 `source_id`。查询历史：`GET /api/v1/telemetry/history?source_id=kepserver-main&point_key=density&limit=100`，后续分页带返回的 `next_before_id`。历史按入库 ID 倒序，接收时间保留在每条记录中；缓存重放意味着入库次序不一定等于测量时间次序。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node scripts\verify_operations.js
.\.venv\Scripts\python.exe -m pytest -q tests/test_training.py
```

全量测试使用临时 SQLite；可选采集依赖安装后才会执行真实本机 OPC UA 协议和证书相关用例，未安装时会明确跳过。CI 已安装锁定采集依赖，避免这些检查被跳过。本机协议测试连接的是临时模拟服务，不代表现场 KEPServer 已通过。

浏览器展示可以使用 `scripts/create_telemetry_demo.py`，该脚本总是创建新的临时数据库并输出其 URL，不连接设备、不读取现场库。把 URL 设为单独测试服务的 `DMCS_DATABASE_URL`，使用不同端口和新浏览器会话查看；演示来源与点位明确标为“演示”。不要把这个 URL 配给正式采集进程。

DS 原版对拍仍使用原始 `data/train_js.json`，不重新生成。下一步应先完成现场点表联调，再实施指标映射和业务取值链改造。
