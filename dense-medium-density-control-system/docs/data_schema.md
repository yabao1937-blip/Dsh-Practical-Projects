# 真实数据表设计（软测量数据接入规范）

> 目标：真实 PLC/化验数据就绪后，按本规范入库即可无缝接入现有训练/预测管线，
> 无需改代码（只改配置与数据）。

## 一、数据流总览

```
PLC/在线仪表 ──┐                          ┌──> indicator_dict（指标字典：定义/来源/周期/范围）
              ├─> realtime_signals ──────┤
化验室/LIMS ──┘   （高频实时，宽表）        └──> lab_assays（低频化验真值，宽表）
                                              │
                    scripts/train.py 自动检测数据库存在 ──> 对齐（窗口聚合）──> 训练/评估/预测
```

- **实时表**：每个采集周期一行，列 = 全部可测特征 + 质量/来源标记
- **化验表**：每个化验样本一行，列 = 全部软测量目标真值
- **指标字典**：定义每列"是什么、从哪来、多快采一次、合理范围、滞后补偿"

## 二、表结构

### 2.1 indicator_dict（指标字典，元数据）

| 字段 | 说明 | 示例 |
|---|---|---|
| code | 指标编码（= 实时/化验表列名） | `clean_ash` |
| name_cn | 中文名 | 精煤真实灰分 |
| category | feature / target / derived | target |
| data_source | plc / online_gauge / lab / lims / derived | lab |
| unit | 单位 | % |
| valid_min / valid_max | 合理范围（数据质量校验用） | 8.0 / 14.0 |
| sample_period_seconds | 采集周期（秒） | 7200 |
| lag_seconds | 滞后补偿（负=实际早于记录时刻） | -90（液位） |
| belt / system_tag | 所属皮带 501/502、系统 401/402/A/B | 501 |
| used_by_targets | 该特征被哪些目标使用 | clean_ash,medium_viscosity |

### 2.2 realtime_signals（高频实时表）

| 列 | 含义 | 来源 | 周期 |
|---|---|---|---|
| timestamp | 采集时刻 `YYYY-MM-DD HH:MM:SS` | — | — |
| raw_ash | 原煤灰分(%) | 在线灰分仪 | 1 分钟 |
| coal_amount | 小时带煤量(t/h) | 皮带秤 | 1 分钟 |
| sys401 / sys402 | 401/402 系统运行(0/1) | PLC 合介泵信号 | 1 分钟 |
| sysA / sysB | A/B 系统运行(0/1) | PLC 合介泵信号 | 1 分钟 |
| desliming473 / desliming474 | 脱粉筛运行(0/1) | PLC | 1 分钟 |
| level | 精磁尾液位(%) | PLC/液位计 | 1 分钟（滞后 -90s） |
| is_stoppage | 停机/低负荷(0/1) | 规则派生 | 1 分钟 |
| quality | 0正常 1可疑 2坏点 | 采集层 | — |
| source | plc / manual / import | — | — |

### 2.3 lab_assays（低频化验表）

| 列 | 含义 | 周期 |
|---|---|---|
| timestamp | 采样时刻（非出结果时刻！） | 2 小时（占位） |
| clean_ash | 精煤真实灰分(%) | 同上 |
| medium_viscosity | 介质粘度(cP) | 同上 |
| clean_moisture | 精煤水分(%) | 同上 |
| suspension_density | 悬液密度(g/cm³) | 同上 |
| recovery | 回收率/产率(%) | 同上 |
| lab_time | 出结果时刻（滞后统计用） | — |
| assay_method / operator | 化验方法 / 化验员 | — |
| quality | 0正常 1可疑 2坏点 | — |

> 重要约定：`timestamp` 一律填**采样时刻**；化验滞后时间由 `lab_time - timestamp`
> 统计，未来做"实时软测量补偿"时使用。

### 2.4 production_batch（可选）

班次/皮带/系统组合表，用于按工况分组统计与特征标注。

### 2.5 model_versions（可选）

模型版本登记表：目标、模型类型、产物路径、指标、训练截止时刻 —— 供后续
控制模型/大模型按版本消费预测结果。

## 三、指标字典种子内容

由 `scripts/init_db.py` 自动生成（从 `config/targets.yaml` 合并 `FEATURE_META` /
`TARGET_META` 补充信息），新增指标时：
1. 在 `config/targets.yaml` 登记特征/目标
2. 在 `scripts/init_db.py` 的 `FEATURE_META` / `TARGET_META` 补充来源、皮带、滞后
3. 重跑 `python scripts/init_db.py`

## 四、真实数据接入步骤

1. **建库**：`python scripts/init_db.py`（生成 `data/db/dmdcs.db` + 指标字典）
2. **采集入库**：
   - PLC/仪表按周期写 `realtime_signals`（可先由现场导出 CSV，用
     `--import-realtime` 导入，或直接写库）
   - 化验/LIMS 写 `lab_assays`（`--import-lab` 导入）
3. **对齐训练**：`python scripts/train.py --rebuild`
   - 自动检测到 `data/db/dmdcs.db` 存在 → 改从数据库读取（`--use-db` 强制，`--csv` 回退）
4. **上线预测**：`python scripts/predict.py --target clean_ash --input ...`
   或库接口 `SoftSensorPredictor`

## 五、采样/化验周期与窗口（占位值，按现场调整）

| 参数 | 占位 | 位置 |
|---|---|---|
| 采样周期 | 5 分钟 | settings.yaml `sample_period_seconds` |
| 化验周期 | 2 小时 | settings.yaml `lab_period_hours` |
| 特征窗口 | 化验前 60 分钟 | settings.yaml `feature_window_minutes` |

## 六、数据质量约定

- 坏点（quality=2）不参与对齐训练（对齐时整行剔除）
- 可疑点（quality=1）默认保留，可在对齐前按需过滤
- 超出指标字典合理范围的值建议标记可疑
- 停机/低负荷时段（is_stoppage=1）的化验样本建议剔除或单独建模
