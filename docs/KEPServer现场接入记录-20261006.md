# KEPServer 现场接入记录

2026-10-06 按用户提供的 `在线仪表关联标签_带id.xlsx`，从远程 KEPServer 读取在线仪表数据，写入本项目数据库并启动独立后台采集。

## 连接与点表

- 现场端点：`opc.tcp://10.255.1.21:49320`，服务器实测为 KEPServerEX 6.4 build 321。
- 网线接口为 `10.255.2.7/24`，现有路由通过 `10.255.2.254` 到达 `10.255.1.0/24`；未修改网卡、路由、防火墙或服务器配置。
- 服务器实测仅提供 `None / None` 端点，匿名会话连接成功。本地配置明确选择 `security: None` 与 `allow_insecure: true`；没有尝试失败后自动降低安全级别。
- 命名空间表实测索引 2 为 `KEPServerEX`。清单中的完整 NodeId 均精确匹配，保留大小写、空格与斜线。
- 清单 `标签清单!A1:F63` 共 54 个有效标签、8 个分组。工作簿“概况”的 283 是保留的旧口径，未用于生成采集配置。第 56、58 行 G:I 的旧快照未导入。
- 首次现场读取 54/54 为 Good，44 个数值点、10 个布尔点，无未知节点或不支持的数据类型。

| 清单分组 | 点位数 |
|---|---:|
| 501/502 皮带秤 | 14 |
| 密度计 | 10 |
| 精磁尾液位 | 1 |
| 重介/浮精/粗精 | 15 |
| 浮精量 | 8 |
| 473 脱粉 | 1 |
| 474 脱粉 | 1 |
| 四系统运行状态 | 4 |
| 合计 | 54 |

表中没有独立数值 ID，标识采用原始 Item ID 与 NodeId。数据库点 key 为 `tag_` 加 Item ID 的 SHA-256 前 20 位；同一 Item ID 重新生成点表时 key 不变。完整 NodeId 保存在点配置和读数中。原始通道、设备、标签、说明、分组及 Excel 行号保存在本地匹配清单。

只有 `JMCWC/LEVEL_B` 的说明明确给出 `%`，其余单位留空，等待现场确认。所有点先按 `scale=1, offset=0` 保存原值，未根据名称猜单位或做量纲换算，也未人为设置未确认的量程。

Good 表示 OPC UA 质量码，不等于已经验证量程或标签业务定义。例如 `TIA_3_3.TIA_3_3.501A/CT_C` 在首次读到约 `3.02×10⁻³³`，网页核对时约 `7.53×10¹⁸`，均为 Good；说明为“班累计带煤量”。这些异常量级保留原值，需现场核对 PLC 地址、标签类型、字节解释和缩放后再用于业务。没有据此修改服务器或猜测换算公式。

## 数据存储

数据库为 `backend/data/dense_medium.db`，三张采集表各自负责：

| 表 | 内容 |
|---|---|
| `telemetry_sources` | 来源在线状态、心跳和采集租约 |
| `telemetry_points` | 54 个点的配置与最新值 |
| `telemetry_readings` | 限频历史，包含原值、值类型对应的数值/布尔值、质量、状态码与三种时间 |

采集每 5 秒更新最新值；历史每点每 60 秒保存一次，质量或问题原因变化时额外保存。当前历史保留 7 天。三种时间为源时间、服务器时间和本机接收时间，存储 UTC，页面按本机时区显示。

入库前使用 SQLite 在线备份 API 生成一致快照，位置记录在 `backend/data/telemetry-setup/before-import.json`。首次入库后逐表核对原业务表的行数与内容校验值，均未改变；`backend/data/train_js.json` 校验值未变。现库没有 `alembic_version`，本次没有执行迁移、stamp 或降级。

采集只读取远程标签，不写 PLC；采集数据目前用于展示和来源核对，业务指标映射及密度建议取值链尚未接入。原业务数据与 DS 模型保持原状。

## 运行与核对

现场配置在 `backend/config/kepserver.local.json`，与下列运行时文件一起忽略 Git：

- `backend/data/telemetry-setup/instrument-tags.json`：原始清单及工作簿校验值。
- `backend/data/telemetry-setup/live-preflight.json`：首次现场匹配、类型与读数核对。
- `backend/data/telemetry-setup/historizing-check.json`：54 个标签的 Historizing 属性核对。
- `backend/data/telemetry-runtime/kepserver-main.json`：后台进程 PID、启动时间、配置和日志路径。
- `backend/data/telemetry-spool/kepserver-main/`：等待入库确认的批次缓存。

在 `backend` 目录运行以下命令可重新启动后台采集；已有来源租约时不会重复启动：

```powershell
.\.venv\Scripts\python.exe scripts\start_collector.py --config config\kepserver.local.json
```

此方式不会随关闭终端或网页退出，尚未安装开机自动运行的系统服务。断网期间采集器会重试；电脑关机或进程退出后需重新启动。检查日志和心跳确认运行，不能只凭配置启用或 PID 判断连接成功。

访问项目的“数据采集与补录”菜单，可查看 54 个点的实时值、质量和时间。接口 `GET /api/v1/telemetry/latest?source_id=kepserver-main` 已实测返回 `connected`、54/54 可用，连续两次核对的最后采样时间前进。

[网页核对截图](verification/kepserver-live-20261006/kepserver-live.png)及[浏览器验收记录](verification/kepserver-live-20261006/browser-result.json)确认 54 行点位和 54/54 可用。验收使用新的浏览器上下文，阻断全部 HTTP 修改请求，仅查询真实读数。`test_collector_cli.py` 与 DS 原版 `test_training.py` 共 12 项通过，DS 对拍基线未重新生成。

## 2026-10-06 停用 501A、502A

按用户要求，从现场采集配置中移除以下四个点位：

| 标签名 | ItemID |
|---|---|
| `501A/COAL_CAP` | `TIA_3_3.TIA_3_3.501A/COAL_CAP` |
| `501A/CT_C` | `TIA_3_3.TIA_3_3.501A/CT_C` |
| `502A/COAL_CAP` | `TIA_3_4.TIA_3_4.502A/COAL_CAP` |
| `502A/CT_C` | `TIA_3_4.TIA_3_4.502A/CT_C` |

当前采集配置为 **50 个物理点位**。本记录前面的 54 点统计、截图及首次入库验收保留为接入时记录。重新启动采集器后，配置之外的四个点位由 `TelemetryRepository.acquire()` 标为禁用；它们不再进入 OPC 读取列表、实时 API 或 `online_instruments` 六列视图。历史表及最后快照保留，沿用原有 7 天历史保留策略，不删除远程 KEPServer 标签。

变更前配置备份、被移除的完整点位定义和停采基准分别记录在忽略 Git 的 `backend/data/telemetry-setup/kepserver-before-disable-501A-502A-*.json`、`disabled-501A-502A.json` 与 `disabled-501A-502A-baseline.json`。复核结果见 [停采核对](verification/disable-501A-502A-20261006/result.json)。

采集器重新连接后，连续两次实测实时接口为 `connected`、50/50 可用，采样时间继续前进；六列视图为 50 行，不含被移除的标签。四个停用点的最新接收时间、历史行数及最后历史记录均与停采基准一致。重启前待入库批次为零，未删除缓存或历史。401、402、A、B 的组合点仍可用。

## 2026-10-06 业务口径确认与实时预测暂缓

用户确认：202 灰分测得的是原煤灰分。此确认明确了测量对象，尚未分别确认 `202/ASH_R_INS`（在线瞬时灰分）、`202/AAD`（化验灰分）与 `202/AAD_T`（累计灰分）的使用口径；三者不能据此互相替代。

以下项目仍待现场核对：

- 累计灰分的计算含义、统计周期和复位规则。尚不确定其是否为算术平均、煤量加权平均或其他计算结果。
- 入洗煤量及班累计、日累计、月累计的数值与业务口径。用户反馈数值不太对，原因尚未确认；需核对计量范围、单位、缩放、统计周期及复位规则。

按用户要求，**暂不接入粗精煤泥灰分实时预测**。本轮仅记录确认事项，未调整采集配置、数值换算或模型输入；已有点位继续保存原始读数。决策总览选点仍只改变采集展示，现有模型计算仍使用业务记录，不能将历史输入产生的预测称为当前实时预测。DS 模型与基线保持原状。

## 远程服务器的历史存储

本次从 OPC UA 读取的是实时标签值。54 个点的 `Historizing` 属性均为 false；这只表明这些节点没有通过当前 OPC UA 接口声明历史化，不能据此判断服务器没有另外保存历史。

KEPServer 可通过 [DataLogger](https://support.ptc.com/help/kepware/features/en/kepware/features/DATALOGGER/Overview.html) 写入 ODBC 数据库，也可通过 [Local Historian](https://support.ptc.com/help/kepware/features/en/kepware/features/localhistorian/GeneralOperation.html) 将历史值、质量和时间保存到本地 `.active` / `.tsd` 归档。后者使用 OPC HDA 提供历史访问。

尚未核对远程服务器的 DataLogger、Local Historian 实际启用状态、数据库连接或磁盘归档路径。当前本项目的 SQLite 历史从本次现场采集开始生成，不能自动补出连接以前或断网期间未读到的数据。
