# 压滤机 A416/A417 点位元数据纠正（设备 YM4_2 / item_id 新通道）· 2026-10-29

## 需求（用户 2026-10-29）

KEPServer 中 A416/A417 压滤机运行状态两点的**设备名应为 `YM4_2`、item_id 应为
`XMZX-YM.YM4_2.A416|A417/YL_STATE`**（昨日迁移只改了 node_id，设备名误写
`XMZX-YM.YM4_2`，item_id 沿用了旧 `TIA_4_2.TIA_4_2.*` 以免断链）。

## 实现（代码 + 主机工具，零断链顺序）

- `backend/app/telemetry/derived.py`：`FLOAT_PRESS_MEMBERS` 的 A416/A417 改用新通道
  item_id（A415/A418 保持 `TIA_4_2.*`），并定义 `PRESS_STATE_ITEM_ALIASES`
  （新↔旧双向映射，同一物理测点）。
- `backend/app/hourly_brief/inputs.py`：`Inputs` 装载点位时按别名双向注册——
  配置里无论写新 id 还是旧 id，组合查找都能解析，**装包与改配置先后任意、零断链**；
  板数折算（PLATE_CNT）不受影响。
- 主机工具 `update_yl_points.py` v3（`UpdateYlPoints.zip` 同名重发）：在原地址迁移
  基础上，把两点 `device=YM4_2`、`item_id=XMZX-YM.YM4_2.*` 按实际结构修正（key 不变，
  历史连续）；幂等（已正确则跳过）、自动备份、停采集→等租约→Start→connected→
  新地址 good 样本校验不变。
- 笔记本本地配置已同步修正（备份 20261029-084727）。

## 验证

- pytest：`test_derived_systems.py` 新增 2 项（成员覆盖新通道+别名双向性；`Inputs`
  按新 id 建点后旧 id 同样解析到同一测点）全过；回归 `test_hourly_brief`/
  `test_decision_sources`/`test_gpt_coarse_amount`/`test_float_output` 共 86+21 项通过
  （含压滤机组合、板数折算、决策链）。
- 笔记本配置实测：两点 device=YM4_2、item_id=XMZX-YM.YM4_2.*、node=新地址。

## 主机操作（顺序：先包后工具）

1. 标准更新包（含 derived.py/inputs.py）→ UpdateProject.cmd；
2. 门户新下载 `yl-points.zip`（v3）→ 解压 → `UpdateYlPoints.cmd`（会提示
   "已按 KEPServer 实际结构修正设备名/item_id"并完成采集重启校验）。
