"""TCN 时序预测器（PyTorch：因果卷积 + 膨胀 + 残差）

职责：预测「下一次化验值」（horizon = 化验周期，默认 2h）。
级联 + 混合输入：
  - 时序通道：原始可测特征（高频，window_steps 步）
  - 静态通道：[软测量当前估计, 最近化验真值]，拼接到 TCN 输出后进线性头

与 XGB/LGBM 的关系：软测量结果作为 TCN 的静态锚点；TCN 专注学习状态随时间的
动态演化，而非重复学习静态「特征 -> 含量」映射。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.preprocessing import StandardScaler


class TemporalBlock(nn.Module):
    """膨胀因果卷积残差块（TCN 基本单元）"""

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int,
                 dilation: int, dropout: float = 0.1):
        super().__init__()
        padding = (kernel_size - 1) * dilation  # 因果：仅在左侧 padding
        self.conv1 = nn.Conv1d(in_ch, out_ch, kernel_size, padding=padding, dilation=dilation)
        self.conv2 = nn.Conv1d(out_ch, out_ch, kernel_size, padding=padding, dilation=dilation)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.downsample = nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch else None
        self._init_weights()

    def _init_weights(self) -> None:
        for conv in (self.conv1, self.conv2):
            nn.init.kaiming_normal_(conv.weight, nonlinearity="relu")
            nn.init.constant_(conv.bias, 0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.relu(self.conv1(x))
        out = self.dropout(out)
        out = self.conv2(out)
        out = out[:, :, -x.size(2):]          # 截断右侧，保持因果对齐
        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)


class TCNModel(nn.Module):
    """堆叠膨胀残差块 -> 取末步 -> 拼接静态特征 -> 线性输出"""

    def __init__(self, in_ch: int, num_filters: int, kernel_size: int,
                 num_layers: int, dropout: float, static_dim: int, horizon: int = 1):
        super().__init__()
        dilations = [2 ** i for i in range(num_layers)]
        blocks = []
        prev = in_ch
        for d in dilations:
            blocks.append(TemporalBlock(prev, num_filters, kernel_size, d, dropout))
            prev = num_filters
        self.tcn = nn.Sequential(*blocks)
        self.head = nn.Linear(num_filters + static_dim, horizon)

    def forward(self, x: torch.Tensor, static: torch.Tensor) -> torch.Tensor:
        h = self.tcn(x)                       # (B, num_filters, W)
        h = h[:, :, -1]                       # 取最后一步 (B, num_filters)
        h = torch.cat([h, static], dim=1)     # 拼接静态特征
        return self.head(h).squeeze(-1)       # (B, horizon) -> (B,)


class TCNForecaster:
    """TCN 前向预测器：fit / predict / save / load

    fit 输入约定：
      X_seq  (N, W, F)  时序通道（原始可测特征）
      static (N, S)     静态通道（[软测量估计, 最近化验真值]，与目标同量纲，用 y 的 scaler 缩放）
      y      (N,)       目标（下一化验值）
    """

    def __init__(self, window_steps: int, num_filters: int = 64, kernel_size: int = 3,
                 num_layers: int = 4, dropout: float = 0.1, lr: float = 1e-3,
                 epochs: int = 150, batch_size: int = 32, patience: int = 20,
                 random_state: int = 42):
        self.window_steps = window_steps
        self.num_filters = num_filters
        self.kernel_size = kernel_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.lr = lr
        self.epochs = epochs
        self.batch_size = batch_size
        self.patience = patience
        self.random_state = random_state

        self.feature_names: List[str] = []
        self.static_names: List[str] = ["soft_estimate", "last_lab"]
        self.model: Optional[TCNModel] = None
        self.scaler_x: Optional[StandardScaler] = None
        self.scaler_y: Optional[StandardScaler] = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.history: Dict[str, List[float]] = {"train_loss": [], "val_loss": []}

    # ---------------- 构建 ----------------
    def _build(self, in_ch: int) -> TCNModel:
        torch.manual_seed(self.random_state)
        np.random.seed(self.random_state)
        model = TCNModel(in_ch, self.num_filters, self.kernel_size, self.num_layers,
                         self.dropout, len(self.static_names), horizon=1)
        return model.to(self.device)

    # ---------------- 缩放 ----------------
    def _scale_x(self, X_seq: np.ndarray) -> np.ndarray:
        n, w, f = X_seq.shape
        return self.scaler_x.transform(X_seq.reshape(n * w, f)).reshape(n, w, f)

    def _scale_static(self, static: np.ndarray) -> np.ndarray:
        # 静态通道与目标同量纲，逐列用 y 的 scaler 缩放
        scaled = np.empty_like(static, dtype=float)
        for j in range(static.shape[1]):
            scaled[:, j] = self.scaler_y.transform(static[:, [j]]).ravel()
        return scaled

    def _tensor(self, arr: np.ndarray) -> torch.Tensor:
        return torch.from_numpy(np.asarray(arr, dtype=np.float32)).to(self.device)

    # ---------------- 训练 ----------------
    def fit(self, X_seq: np.ndarray, static: np.ndarray, y: np.ndarray,
            X_val: Optional[np.ndarray] = None, static_val: Optional[np.ndarray] = None,
            y_val: Optional[np.ndarray] = None) -> "TCNForecaster":
        if not self.feature_names:  # 未显式指定时用整数占位（trainer 会覆盖为真实列名）
            self.feature_names = list(range(X_seq.shape[2]))
        n, w, f = X_seq.shape
        # 标准化（仅用训练集拟合）
        self.scaler_x = StandardScaler().fit(X_seq.reshape(n * w, f))
        self.scaler_y = StandardScaler().fit(y.reshape(-1, 1))

        Xs = self._scale_x(X_seq)
        static_s = self._scale_static(static)
        ys = self.scaler_y.transform(y.reshape(-1, 1)).ravel()

        self.model = self._build(f)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        criterion = nn.MSELoss()

        Xt = self._tensor(Xs).transpose(1, 2)  # (N, W, F) -> (N, F, W) 通道优先
        St, yt = self._tensor(static_s), self._tensor(ys)
        dataset = torch.utils.data.TensorDataset(Xt, St, yt)
        loader = torch.utils.data.DataLoader(dataset, batch_size=self.batch_size, shuffle=True)

        has_val = X_val is not None and static_val is not None and y_val is not None
        Xv = Sv = yv = None
        if has_val:
            Xv = self._tensor(self._scale_x(X_val)).transpose(1, 2)
            Sv = self._tensor(self._scale_static(static_val))
            yv = self._tensor(self.scaler_y.transform(y_val.reshape(-1, 1)).ravel())

        best_val = float("inf")
        best_state = None
        patience_left = self.patience

        for epoch in range(1, self.epochs + 1):
            self.model.train()
            total_loss = 0.0
            for xb, sb, yb in loader:
                optimizer.zero_grad()
                loss = criterion(self.model(xb, sb), yb)
                loss.backward()
                optimizer.step()
                total_loss += loss.item() * len(xb)
            train_loss = total_loss / len(dataset)
            self.history["train_loss"].append(train_loss)

            if has_val:
                self.model.eval()
                with torch.no_grad():
                    val_loss = criterion(self.model(Xv, Sv), yv).item()
                self.history["val_loss"].append(val_loss)
                if val_loss < best_val:
                    best_val = val_loss
                    best_state = {k: v.detach().cpu().clone() for k, v in self.model.state_dict().items()}
                    patience_left = self.patience
                else:
                    patience_left -= 1
                    if patience_left <= 0:
                        break
            else:
                best_state = {k: v.detach().cpu().clone() for k, v in self.model.state_dict().items()}

        if best_state is not None:
            self.model.load_state_dict(best_state)
        return self

    # ---------------- 预测 ----------------
    def predict(self, X_seq: np.ndarray, static: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("模型未训练，请先调用 fit")
        self.model.eval()
        Xs = self._scale_x(X_seq)
        static_s = self._scale_static(static)
        with torch.no_grad():
            out = self.model(self._tensor(Xs).transpose(1, 2), self._tensor(static_s)).cpu().numpy()
        return self.scaler_y.inverse_transform(out.reshape(-1, 1)).ravel()

    # ---------------- 持久化 ----------------
    def save(self, directory: str) -> None:
        os.makedirs(directory, exist_ok=True)
        torch.save(self.model.state_dict(), os.path.join(directory, "tcn_state.pt"))
        joblib.dump({"scaler_x": self.scaler_x, "scaler_y": self.scaler_y,
                     "feature_names": self.feature_names,
                     "static_names": self.static_names},
                    os.path.join(directory, "tcn_scalers.joblib"))
        joblib.dump(self._config(), os.path.join(directory, "tcn_config.joblib"))

    @classmethod
    def load(cls, directory: str) -> "TCNForecaster":
        cfg = joblib.load(os.path.join(directory, "tcn_config.joblib"))
        obj = cls(**cfg)
        obj.feature_names = joblib.load(os.path.join(directory, "tcn_scalers.joblib"))["feature_names"]
        obj.static_names = joblib.load(os.path.join(directory, "tcn_scalers.joblib"))["static_names"]
        obj.scaler_x = joblib.load(os.path.join(directory, "tcn_scalers.joblib"))["scaler_x"]
        obj.scaler_y = joblib.load(os.path.join(directory, "tcn_scalers.joblib"))["scaler_y"]
        obj.model = obj._build(len(obj.feature_names))
        obj.model.load_state_dict(torch.load(os.path.join(directory, "tcn_state.pt"),
                                             map_location=obj.device, weights_only=True))
        obj.model.eval()
        return obj

    def _config(self) -> Dict:
        return {
            "window_steps": self.window_steps, "num_filters": self.num_filters,
            "kernel_size": self.kernel_size, "num_layers": self.num_layers,
            "dropout": self.dropout, "lr": self.lr, "epochs": self.epochs,
            "batch_size": self.batch_size, "patience": self.patience,
            "random_state": self.random_state,
        }
