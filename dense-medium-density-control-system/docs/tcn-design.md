# TCN 时序预测设计（预测模型层 · 第二步）

> 目标：在 XGB/LGBM 软测量基础上，用 TCN 预测「未来物质信息」。
> 状态：已实现，待 torch 安装后端到端验证。

## 1. 定位与职责划分

| 模型 | 职责 | 输出 |
|---|---|---|
| XGB / LGBM | 软测量：用实时可测变量推断**当前**不可测指标 | 当前目标估计 |
| TCN | 时序预测：预测**未来**的目标值 | 下一次化验值（horizon=2h） |

## 2. 架构：级联 + 混合（已确认决策）

```
实时可测特征(高频) ─┬─> XGB/LGBM 软测量 ──> 当前估计 ──┐（静态通道）
                    │                                  ├─> TCN ──> 未来目标
最近化验真值(稀疏) ─┴────────────────────────────────────┘（静态通道）
```

**TCN 输入**：
- 时序通道：过去 `window_steps`（默认 48 步 = 4h）的原始可测特征
- 静态通道：`[软测量当前估计, 最近化验真值]`（拼接到 TCN 末层输出后进线性头）

**理由**：软测量提供"当前真实状态"的最佳实时估计，TCN 专注学习动态演化，避免重复学习静态映射。

## 3. 关键数据问题与方案

化验真值 2h 一针（稀疏），TCN 训练标签来自哪里？→ **方案 A：预测下一次化验值**：
- 样本 k：输入 = T_k 前的特征窗口 + 软测量(T_k) + 化验值 y(T_k)；目标 = y(T_{k+1})
- 监督充分、业务直接（提前 2h 知道下个灰分结果）
- 备选方案（未做）：B 细粒度多步序列（需软测量伪标签）、C 间接特征预测

## 4. 无泄漏级联

软测量输入只在 TCN **train 段**拟合，再对全样本生成估计：
- val/test 段软测量从未见过其真值 → 无泄漏
- train 段为样本内估计 → v1 已知简化（P2 用 walk-forward 逐步生成消除）

## 5. 模型结构

自定义轻量 TCN（PyTorch）：`膨胀因果卷积 + 残差块 → 取末步 → 拼接静态特征 → 线性头`
- 膨胀 [1,2,4,8]（num_layers=4）、通道 64、kernel 3、dropout 0.1
- 损失 MSE、Adam、早停于验证集；输入/目标用 StandardScaler（仅训练集拟合）
- 持久化：`tcn_state.pt`（state_dict）+ `tcn_scalers.joblib` + `tcn_config.joblib`（跨环境更稳）

## 6. 产物与命令

```
artifacts/tcn/<target>/  tcn_state.pt / tcn_scalers.joblib / tcn_config.joblib / meta.json / report.json
```

```bat
python scripts\train_tcn.py                    # 训练 tcn.enabled_targets
python scripts\predict_tcn.py --target clean_ash --realtime data\raw\realtime.csv --last-lab 10.85
```

## 7. 评估口径

测试集（时间切分）对比：TCN vs 持久化基线（上一化验值）vs 训练均值；指标 R²/RMSE/MAE/MAPE。
