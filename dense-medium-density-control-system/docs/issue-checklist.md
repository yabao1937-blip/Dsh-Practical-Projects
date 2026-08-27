# 问题与待优化清单（2026-08-26 第一步开发日）

> 记录今日发现/修复的问题、待优化点与后续需求，供真实数据接入前后跟踪。
> 优先级：P0 阻塞真实数据接入；P1 框架完善；P2 工程化/后续步骤。

## 一、今日已解决

| # | 问题 | 状态 |
|---|---|---|
| 1 | 依赖包安装卡死：沙箱环境无外网（pip 20 分钟 0 连接、缓存不增长） | ✅ 改用国内镜像由用户手动安装 |
| 2 | 包装错环境：最初装进 PythonProject10\.venv1（PyCharm 旧配置指向它） | ✅ 已装回项目自身 .venv（版本与 requirements-pinned.txt 一致，导入验证通过） |
| 3 | Windows GBK 控制台打印 R² 崩溃（UnicodeEncodeError） | ✅ 4 个脚本加控制台 UTF-8 重配置 |
| 4 | 端到端管线（demo 生成→训练→评估→预测，CSV 与数据库双路径） | ✅ 全部跑通，两路径指标一致 |
| 5 | xgboost 跨版本模型不兼容（3.2.0 训练的 joblib 在 2.1.4 下加载预测乱值，R²=-270） | ✅ 用项目 .venv（锁定版）重训，指标恢复一致 |

## 二、P0 —— 真实数据接入前必须确认/完成

- [ ] **预测目标清单确认**：targets.yaml 当前 5 个软测量目标（精煤真实灰分/介质粘度/精煤水分/悬液密度/回收率）是否为最终清单？新增/删减只改 yaml + 重跑 init_db
- [ ] **现场占位值确认**：采样周期（现 5min）、化验周期（现 2h）、特征窗口（现 60min）、液位滞后补偿（-90s）——按现场实际修正 settings.yaml
- [ ] **真实数据入库**：PLC/仪表写 realtime_signals，化验/LIMS 写 lab_assays（模板见 data/templates/）
- [ ] **验收精度线**：与工艺确认（如 clean_ash 预测 MAE ≤ 0.1%、命中率等），真实数据出基线后定标

## 三、P1 —— 框架完善

- [ ] **集成方式优化**：demo 上 LGBM 单模型优于加权集成（weighted 被 XGB 拖累，clean_ash RMSE 0.329 vs 0.404）→ 评估 `ensemble.method: best` 或引入 stacking
- [ ] **回收率难预测**（R²≈0.38，LGBM 为负）→ 补充特征：悬液密度、灰分仪读数、煤质可选性数据；或调整化验口径
- [ ] **质量标记落地**：对齐时按 quality 过滤（bad 剔除/可疑保留）与指标字典合理范围校验（当前仅 dropna）
- [ ] **滞后补偿落地**：indicator_dict.lag_seconds 已登记（如 level=-90s），对齐模块需实际应用
- [ ] **特征窗口扩展**：当前 last/mean/slope → 增加 std、分位数、滞后多期等聚合
- [ ] **超参调优**：当前为默认值 → Optuna/网格搜索，按目标分别调
- [ ] **SHAP 可解释性**：内置 feature_importance 已有，加 SHAP 给工艺人员解释"什么在推高灰分"
- [ ] **模型版本登记**：model_versions 表已建未写入 → 训练时登记（目标/模型/产物路径/指标/训练截止时刻）

## 四、P2 —— 工程化 / 后续步骤

- [ ] **模型序列化兼容性**：joblib/pickle 跨 xgboost 版本不兼容（换环境必须重训）；后续可改用 `Booster.save_model` 原生格式导出，或严格锁定训练/预测环境版本一致
- [ ] **TCN 级联无泄漏化**：当前 v1 软测量输入仅在 TCN train 段拟合（val/test 无泄漏），train 段为样本内估计；后续可用 walk-forward 逐步生成软测量序列彻底消除样本内乐观
- [ ] **TCN 训练标签稀疏**：当前预测「下一次化验值」（horizon=2h），样本量受化验次数限制（~540）；若需细粒度未来序列，需软测量伪标签（弱监督）
- [ ] **单元测试**：split / features / trainer / metrics / tcn 关键逻辑
- [ ] **在线预测封装**：当前 predict 输入需"已聚合宽表"；在线场景需封装"实时窗口 → build_window_features → predict"一步接口
- [ ] **日志中文编码统一**：部分终端仍显示乱码，建议 logging 统一 UTF-8
- [ ] **demo 数据清理**：dmdcs.db 当前为 demo 数据（12960+540 行），真实数据接入前清空重建（init_db 重建或 DELETE）
- [ ] **旧项目数据迁移**（可选）：旧 app.db / Excel → 新 schema
- [x] **TCN 时序预测（第二步）**：✅ 已实现并验证（级联+混合，预测下一次化验值）；clean_ash 测试 RMSE 0.405 vs 持久化基线 0.451，medium_viscosity 0.255 vs 0.267，均优于基线
- [ ] **后续模块**：TCN 加入集成（三模型对比）→ 异常模型（工艺规则+Isolation Forest+TCN 残差）→ 控制模型（PID/MPC）→ 大模型（本地+RAG+工具调用）→ PLC 执行
