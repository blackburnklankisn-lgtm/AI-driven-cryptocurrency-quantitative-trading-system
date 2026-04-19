# MLPredictor（机器学习预测器）完整管线深度分析与优化方案

---

## 一、当前实现架构总览

整个 ML 管线由 **6 个模块** 组成，形成完整的「训练 → 推理 → 自适应」闭环：

```
MLFeatureBuilder → ReturnLabeler → WalkForwardTrainer → SignalModel → MLPredictor → ContinuousLearner
    (特征工程)       (标签生成)       (时序验证训练)       (模型封装)     (实时推理)     (持续学习)
```

### 1. 特征工程 — feature_builder.py

构建 **6 大类特征**：

| 类别 | 具体内容 | 数量（默认配置） |
|------|---------|-----------------|
| 技术指标 | SMA(10/20/50), EMA(12/26), RSI(14), ATR(14), BB(20), MACD(12,26,9) | ~15 |
| 收益率特征 | close_return, open_return, hl_range, close_to_high, close_to_low | 5 |
| 滞后特征 | 6 个基础指标 × 5 个滞后期(1,2,3,5,10) | 最多 30 |
| 滚动统计 | mean/std/skew × 3 个窗口(5,10,20) | 9 |
| 均线相对位置 | price_vs_sma_N, sma_10_20_spread | ~4 |
| 时间特征 | hour/dow sin/cos（默认关闭） | 0/4 |

总计约 **60+ 个特征**。

**防未来函数保证**：所有 `shift()` 只允许正值（向后看），`rolling()` 强制 `min_periods=window`。

### 2. 标签生成 — labeler.py

- **`label_binary()`**：核心标签方法，面向现货多头
  - 未来 5 根 K 线对数收益率 > 0.5% → **1（买入）**
  - 其余（含小涨/横盘/下跌）→ **0（不买）**
  - 最后 `forward_bars=5` 行强制 NaN（无完整未来数据）
- **Embargo 机制**：`check_no_leak()` 验证训练集/测试集之间的时序隔离

### 3. 模型 — model.py

`SignalModel` 封装 sklearn Pipeline（`StandardScaler → Classifier`），支持 3 种基模型：

| 模型 | 优先级 | 特点 |
|------|-------|------|
| **LightGBM** | 首选 | 梯度提升树，支持 early stopping，效率高 |
| **RandomForest** | 后备 | `class_weight=balanced`，鲁棒不需调参 |
| **LogisticRegression** | 基线 | 线性模型，快速验证特征有效性 |

关键设计：LightGBM 配置了 `lambda_l1=0.1, lambda_l2=0.1`（正则化），`min_child_samples=30`（防过拟合），`feature_fraction=0.8, bagging_fraction=0.8`（随机化）。

### 4. Walk-Forward 训练 — trainer.py

```
时间轴 ──────────────────────────────────────────────►
第 1 折：[=====TRAIN=====][EMB][==TEST==]
第 2 折：[========TRAIN========][EMB][==TEST==]
第 3 折：[===========TRAIN===========][EMB][==TEST==]
```

- **Expanding Window**（默认）：训练集逐折扩大
- **Embargo 隔离**：训练集末尾 `forward_bars=5` 行丢弃
- **评估指标**：Accuracy, F1, Precision, Recall, AUC-ROC
- **输出**：各折 OOS 指标 + 特征重要性 + 最终模型

### 5. 实时推理 — predictor.py

`MLPredictor` 继承 `BaseAlpha`，与 MACross/Momentum 接口完全一致：

```python
on_kline(event) → List[OrderRequestEvent]
```

核心逻辑：

- **预热保护**：缓冲区 < 300 条时不出信号
- **推理**：每根 K 线取缓冲区最后一行特征 → `model.predict_signal_proba()` → 得到 P(买入)
- **信号过滤**：
  - P(买入) ≥ 0.60 且未持仓 → **买入**
  - P(买入) ≤ 0.40 且持仓中 → **卖出**
- **冷却期**：信号触发后 5 根 K 线内不再产出同方向信号
- 固定下单量 `order_qty=0.01`

### 6. 持续学习 — continuous_learner.py

3 种重训触发机制：

1. **定时**：每 500 根 K 线强制重训
2. **性能退化**：近期 OOS 精度 < 55% 触发
3. **概念漂移**：KS 检验发现 > 30% 的特征分布显著变化（p < 0.05）

模型切换规则：新模型 Accuracy 高 > 1% **或** F1 高 > 2% 才替换旧模型，否则保守保留。

---

## 二、策略逻辑核心评价

### 优点（设计水准较高）

1. **防未来函数做得严谨**：Labeler 强制末尾 NaN + Embargo + `check_no_leak()`，三层保护
2. **Walk-Forward 是金融 ML 的正确验证方法**，避免了 KFold 等时序泄露陷阱
3. **Pipeline 封装** 保证了 Scaler 参数与模型参数一起持久化，推理时不会出现归一化不一致
4. **ContinuousLearner 的 KS 漂移检测** 是专业级别的方案，crypto 市场 regime 变化快，这个设计非常对症
5. **特征工程覆盖面广**：技术指标 + 滞后 + 滚动统计 + 相对位置，维度丰富

### 问题与不足

| 编号 | 问题 | 严重程度 | 详细分析 |
|------|------|---------|---------|
| **P1** | 每根 K 线重建完整特征矩阵 | **高** | `_infer_buy_probability()` 每次调用都将 300 行缓冲区转 DataFrame → `feature_builder.build()` → 取最后一行。300 行 × 60+ 特征列 × 每小时一次 = 巨大浪费 |
| **P2** | 固定下单量 0.01 BTC | **高** | 不随账户权益和波动率缩放，与已实现的 PositionSizer 模块脱节 |
| **P3** | 仅用二分类（涨/不涨），丢失做空信号 | **中** | label_binary 把 -1 归入 0，在现货市场合理，但如果有空头能力（合约）就是浪费 |
| **P4** | 特征维度过高（60+），无特征选择 | **中** | 小样本场景容易过拟合。forward_bars=5 加 embargo 后有效样本量显著减少 |
| **P5** | 阈值硬编码（0.60/0.40） | **中** | 最优阈值应该由 Walk-Forward 的 OOS 概率分布决定，而非固定值 |
| **P6** | 冷却期是固定 5 根 K 线 | **低** | 无法适应不同波动环境。高波动时 5h 太短，低波动时 5h 太长 |
| **P7** | `_in_position` 仅靠自身状态管理 | **中** | 如果外部止损/风控强制平仓，MLPredictor 的 `_in_position` 不会同步 |
| **P8** | ContinuousLearner 未集成到主循环 | **中** | 当前只是独立模块，没有与 `apps/trader/main.py` 挂钩 |

---

## 三、可参考的业界方案

| 方向 | 方案 | 说明 |
|------|------|------|
| 特征选择 | **Boruta / SHAP importance + 递归剔除** | 减少无效特征，降低过拟合风险 |
| 概率校准 | **Platt Scaling / Isotonic Regression** | sklearn `CalibratedClassifierCV`，使 predict_proba 输出的概率更可靠 |
| 自适应阈值 | **在 Walk-Forward 每折 OOS 上用 Youden's J 找最优阈值** | 替代硬编码 0.60 |
| 增量推理 | **只计算增量特征**，维护 rolling state | 避免每根 K 线重建完整 DataFrame |
| 集成学习 | **多模型投票（RF + LightGBM + LR）** | 降低单一模型偏差 |
| 元标签 | **Meta-Labeling**（Marcos López de Prado） | 用第二个模型判断"第一个模型的信号是否可信"，提高精度 |
| 样本权重 | **时间衰减加权 / 难例挖掘** | 让近期样本有更高权重 |

---

## 四、推荐优化方案

按优先级排列：

### P0：推理性能优化 — 增量特征计算

**问题**：每根 K 线都对 300 行数据 `build()` 完整特征矩阵，O(N × F) 计算量。

**方案**：维护增量状态，只对新 K 线追加计算。

```python
# 概念示意：缓存特征矩阵，每次只追加最后一行
def _infer_buy_probability(self) -> Optional[float]:
    new_row = self._ohlcv_buffer[-1]
    self._feat_cache.append(self._compute_single_row_features(new_row))
    last_features = self._feat_cache[-1]
    if any_nan(last_features):
        return None
    return float(self.model.predict_signal_proba(last_features))
```

**理由**：加密市场多品种场景下，每个品种每小时省去 300 行 × 60 列的重复计算，CPU 开销降低约 95%。

### P0：接入 PositionSizer 动态仓位

**问题**：`order_qty=0.01` 固定不变。

**方案**：把推理概率作为置信度权重，结合 ATR 波动率目标仓位：

$$
Qty = f(P_{buy}, ATR, Equity) = \frac{confidence \times TargetRisk \times Equity}{ATR\% \times Price}
$$

其中 $confidence = (P_{buy} - threshold) / (1 - threshold)$，取值 [0, 1]，概率越高仓位越大。

**理由**：与系统已有的 PositionSizer 模块对齐，避免"高置信 + 低仓位"和"低置信 + 高仓位"的错配。

### P1：自适应阈值替代硬编码

**问题**：`buy_threshold=0.60` 和 `sell_threshold=0.40` 无数据支撑。

**方案**：在 Walk-Forward 每折 OOS 上，用 **Youden's J 统计量** 找最优阈值：

$$
J = Sensitivity + Specificity - 1
$$

$$
threshold^* = \arg\max_{t} J(t)
$$

将每折最优阈值取均值/中位数，作为实盘阈值。

**理由**：不同市场阶段、不同模型类型的最优分界点差异很大。0.60 可能在 RF 上合理，但在 LightGBM 上可能过保守或过激进。

### P1：概率校准（Platt Scaling）

**问题**：树模型的 `predict_proba()` 输出不一定是真实概率，可能系统性偏高或偏低。

**方案**：在 Pipeline 后加 `CalibratedClassifierCV`：

```python
from sklearn.calibration import CalibratedClassifierCV
calibrated = CalibratedClassifierCV(base_model, method='sigmoid', cv=3)
```

**理由**：这样当模型说"70% 概率上涨"时，实际上涨概率确实接近 70%，阈值过滤才有统计意义。

### P2：特征选择 — 基于 SHAP 重要性剔除

**问题**：60+ 特征中很多是高度相关的（如 sma_10_lag1 和 sma_10_lag2），增加过拟合风险。

**方案**：

1. 训练后用 SHAP 计算每个特征的全局重要性
2. 剔除 SHAP = 0 的特征
3. 对相关系数 > 0.95 的特征对，保留 SHAP 更高的那个

目标：从 60+ 压缩到 20-30 个核心特征。

**理由**：加密市场的 1h K 线数据量有限（2000 行有效样本 / 60 特征 ≈ 33 样本/特征），已低于经验法则建议的 50:1。

### P2：仓位状态同步 — 从主循环获取持仓

**问题**：`_in_position` 独立维护，外部止损后不同步。

**方案**：由 `main.py` 在每个 polling 周期把当前持仓量注入 strategy：

```python
strategy.sync_position(current_quantity)  # 在 on_kline 之前调用
```

**理由**：当前系统已有入场价止损和熔断器，都可能在策略不知情的情况下清仓。

---

## 五、硬件资源要求分析（16GB 内存是否足够）

### 各环节内存估算

| 环节 | 峰值内存 | 说明 |
|------|---------|------|
| Python 进程基线 | ~150MB | 解释器 + numpy/pandas/sklearn import |
| MLPredictor 推理（×3 品种） | ~60MB | 每次 300 行 × 60 列 float64 ≈ 140KB/次，3 品种峰值叠加 |
| 训练 WalkForwardTrainer | ~300-500MB | 2000 行 × 60 特征 × 5 折，RF 200 棵树 max_depth=8 |
| LightGBM 训练（如果启用） | ~400-700MB | 300 棵树，内部 histogram 结构 |
| 6 个规则策略 (MACross+Momentum) | ~20MB | deque 缓冲区很小 |
| FastAPI + CCXT | ~80MB | WebSocket 连接 + HTTP 服务 |
| Electron 桌面端 | ~300-500MB | Chromium 引擎 + Node.js |
| OS + 其他 | ~2-4GB | Windows 系统自身 |

**正常运行总计**：约 **4-6GB**，16GB 完全足够。

### 需要关注的隐患：ContinuousLearner 内存泄漏

`continuous_learner.py` 中 `_ohlcv_buffer` 使用的是无上限的 `List`（对比 `predictor.py` 的有界 `deque(maxlen=400)`），长期运行会持续增长：

| 运行时长 | 累积 K 线数（3 品种 × 1h） | 缓冲区内存 |
|---------|--------------------------|-----------|
| 1 周 | ~500 | ~3MB |
| 1 月 | ~2,200 | ~13MB |
| 6 月 | ~13,000 | ~78MB |
| 1 年 | ~26,000 | ~156MB |
| 重训时峰值（1 年数据） | 26K 行 × 60 特征 × 5 折拷贝 | ~1.2GB |

建议给 `_ohlcv_buffer` 加上 maxlen 上限以防止长期内存增长。

### 结论

| 项目 | 16GB 够用？ | 备注 |
|------|------------|------|
| 训练（RF, 2000 行） | ✅ 完全够 | 峰值 ~500MB |
| 训练（LightGBM, 2000 行） | ✅ 完全够 | 峰值 ~700MB |
| 实时推理（3 品种） | ✅ 完全够 | 每次 ~20MB |
| ContinuousLearner 长期运行 | ⚠️ 够用但需监控 | 建议加 maxlen |
| 全系统 + Electron + 日常应用 | ✅ 可用 | 总占用 ~6GB |

**16GB 是合理的最低配置**，无需升级。8GB 也能跑但训练 + Electron 同时运行时会比较紧张。

---

## 六、总结评价

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构完整度 | ★★★★☆ | 训练→推理→持续学习全链路实现，组件解耦清晰 |
| 防泄露严谨性 | ★★★★★ | Embargo + NaN 截断 + check_no_leak 三层防护，超出多数同类系统 |
| 生产就绪度 | ★★★☆☆ | 核心逻辑完善，但推理性能、持仓同步、阈值校准还需补齐 |
| 策略竞争力 | ★★★☆☆ | Binary 二分类 + RF/LightGBM 属于主流方案，但缺乏 Meta-Labeling 等进阶手段 |
| 可扩展性 | ★★★★☆ | BaseAlpha 接口一致，加新模型只需注册，ContinuousLearner 自带版本管理 |

**总体结论**：这是一个设计扎实的 ML 策略管线，在防未来函数和时序验证方面做得非常专业。核心短板在于**推理层的实用性**（性能、仓位、阈值），建议按 P0 → P1 → P2 优先级逐步补齐。

---

## 七、优化实施优先级

### P0（立即）

1. 推理性能优化 — 增量特征计算（降低 CPU 开销 ~95%）
2. 接入 PositionSizer 动态仓位（消除固定 0.01 BTC 的风险错配）

### P1（短期）

3. 自适应阈值 — Youden's J 替代硬编码 0.60/0.40
4. 概率校准 — Platt Scaling 使 predict_proba 输出贴近真实概率

### P2（中期）

5. 特征选择 — SHAP 重要性剔除冗余特征（60+ → 20-30）
6. 仓位状态同步 — 从主循环获取实际持仓，消除止损后状态不一致

### P3（远期）

7. ContinuousLearner 集成到主循环
8. ContinuousLearner `_ohlcv_buffer` 加 maxlen 防止内存泄漏
