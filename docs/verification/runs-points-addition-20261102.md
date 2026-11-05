# 脱粉 RUNS 两点新增与发布（2026-11-02）

状态：**主机已安装，采集和 60 秒入库验收通过**，用户原始回执为 `installed`。现场设备含义仍待明日核对；当前 False 是有效布尔观测，不要求先变 True 才算安装成功。

## 类型证据与新增范围

生产只读审计代理对两条指定 NodeId 做了一次只读探测：均为 DataType `i=1`（namespace 0）、VariantType `Boolean`、Python `bool=False`、StatusCode `0 / Good`，没有 browse 或 PLC 写入。Source/Server 为 `2026-11-02T07:52:13.111Z`，笔记本接收为 `07:53:02.520Z`；不据跨机时差判断陈旧，也不从瞬时 False 推断长期停机。证据：`output/desliming-operation-audit-20261102/runs-direct-probe.json`。

| 名称 | NodeId | 独立稳定 key |
| --- | --- | --- |
| 473运行反馈（RUNS） | `ns=2;s=TIA_3_3.TIA_3_3.473/RUNS` | `tag_881030838dd4d05ebcda` |
| 474运行反馈（RUNS） | `ns=2;s=TIA_3_3.TIA_3_3.474/RUNS` | `tag_04a29ab776cb07869148` |

两点均按已证 Boolean 原值采集，scale=1、offset=0、max_age_seconds=30，不做位图或设备运行含义推断。仅追加到目标主机现有 points 数组，保留全部旧字段及非 points 字节，不携带全量配置、账户秘密、数据库或模型。原 `473/if_prod`、`474/if_prod` 的 key/node/type 不替换，决策绑定、EXPERT8/C7/E5 与小时 context 不变；主机 5 秒采集、60 秒普通历史、30 秒液位策略保留。本机实际配置未写入或启动采集。

## 实现与验证

实现：`backend/scripts/host_runs_points/add_runs_points.py`、`AddRunsPoints.cmd`，构建器 `backend/scripts/package_runs_points.py`。只备份配置，复用已审 owned-collector 停止、自然 lease 等待和新 owner/两次提交推进校验；仅用 `start_collector.py` 恢复采集，Web/8000 不重启，无需 7GB 数据库备份。失败恢复精确旧配置并保留 DB/spool；遇未知外部配置改动不覆盖、不盲目启动。重复运行也须只读验证当前 owner/lease/connected 及四个新旧点近 180 秒 fresh good，不能因定义已存在就报成功。

新增测试 `backend/tests/test_runs_points_update.py`：**33 passed in 1.39s**，主代理独立重跑 **33 passed in 1.62s**，不重复计数。实际 ZIP 的独立合成安装/失败恢复/幂等/冲突/False-good 与 bad-status 校验 harness 最终 exit 0。另用真实应用与隔离 SQLite 验证同名、反值 RUNS 追加及两种数组顺序：旧脱粉组合、默认/显式绑定、八因子输入与 GPT 结果仍使用原 if_prod，未因名称/设备编号出现 last-wins 自动替换。

验样按 source+point+received_at 索引 LIMIT 1，使用既有只读 metadata catalog 解码，检查布尔类型、Good 质量、status 严重位及内外 key/时间一致；False 是有效成功值。

## 发布与运行影响

- 独立包：`output/runs-points-20261102/AddRunsPoints.zip`，**25,425 字节**，SHA-256 `baa6bac89d74b5a39e5f3fb82ac9c87ce244ee53e3ba0a0fb38fd16e2a771d90`。7 成员为 bundle manifest 与 6 个工具/两点 delta 文件，无业务源码、全量配置或模型载荷；回执版本独立为 `runs-points-20261102-v1`。
- 无 secret 直链：[新增脱粉 RUNS 点位包](http://10.255.2.6:8765/runs-points.zip)。页面标签为“下载新增脱粉 RUNS 点位包（AddRunsPoints.zip）”，入口 `AddRunsPoints.cmd`。实际 HTTP 200、Content-Length=25425、下载 SHA 与上述完全一致。
- 仅给本机下载门户新增独立 route/标签；按监听 PID、脚本和输出根目录核归属后，以 Hidden 重载下载器。现有 8 个 ZIP 路由均 HTTP 200、原大小保留，旧项目更新包、压滤机点位包、settings/latest 哈希不变；不影响主机/本机 8000。
- **安装会短暂停采集**，包括停止进程、等待自然 lease、启动及提交验证；采集 owner 更换后还需约 **90 秒历史 ring 重建**，即时窗口可能暂时受影响，不能称完全无运行影响。网页保持运行；数学输入、模型绑定与小时 context 规则不变。

发布证据：`output/runs-points-20261102/publication.json`、`bundle.redacted.json`。本轮安装与最小生产只读验收已闭环；今日不据 RUNS 切换模型输入，明日再由现场对照其真实设备含义。

## 安装后最小只读验收

- 生产只读审计代理核对主机北京时间 `2026-11-02 16:40:15.798`：source `connected`、`error=null`，当前批次 **30 点**，`poll_seconds=5`、普通 `history/cadence=60`。
- 两路新 RUNS 均为原始 `bool=False`、`quality=good`、`status_code=0`、`issue=null`，接收时间均为 `16:40:11.361`，相对同机 checked_at 的年龄为 **4.437 秒**。
- 每点最近抽查的三条历史均为 `periodic / cadence_seconds=60 / good`：`16:38:01.164 → 16:39:01.382 → 16:40:01.361`，相邻间隔 **60.218 / 59.979 秒**；各自 definition hash 与对应 live 样本相同。这是最近三条抽查，不把 `16:38` 称为首次产生数据的时刻。
- 本次只读检查未重新核验旧 if_prod 或液位 30 秒，也未取得旧 owner 作独立比较。当前 owner 为 `b4c94359842641eba18c791a10fe4acc`，不据其单个现值宣称独立证明 owner 已更换。旧点和液位策略的保留仍按既有代码审计、隔离集成及用户安装回执记录，不扩写为本次生产读回实测。

本次仅同步报告，没有再次访问主机、改包、改服务或追加测试。
