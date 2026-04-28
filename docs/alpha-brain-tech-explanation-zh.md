# Alpha Brain 页面技术说明

> **文档目的**：说明 Alpha Brain 页面每个栏位的真实计算链，防止字面意思造成的误读。  
> 本文档与代码版本同步，修改相关模块时应同步更新本文档。

---

## 0. 全局架构概述

Alpha Brain 页面的数据由三条独立计算链汇聚而成：

```
① 市场状态识别链
   HTX K 线 (1h) → RegimeFeatureSource → HybridRegimeScorer → RegimeState

② 策略编排链
   RegimeState + 策略信号 → StrategyOrchestrator → GatingEngine → OrchestrationDecision

③ 自适应学习链（可选，需 ML 模型文件）
   OHLCV 历史 → ContinuousLearner → WalkForwardTrainer → MLPredictor
```

**快照刷新方式**：后端 `_build_alpha_brain_snapshot()` 读取 trader 运行时的内存缓存，不是每次刷新都重新计算。  
**数据来源**：SOL/USDT 的 1h K 线从 HTX 交易所实时拉取（WebSocket 推送 + 每 5 秒 fetch_ticker 补偿）。

---

## 1. 主导市场状态 & 置信度

### 1.1 展示字段

| 字段 | 代码来源 |
|------|---------|
| 主导市场状态 | `RegimeState.dominant_regime` |
| 置信度 X% | `RegimeState.confidence` |
| 状态稳定：是/否 | `_latest_regime_stable`（最近5根bar的dominant_regime是否全部一致） |

### 1.2 计算链

**步骤一：原始数据**  
系统启动时预加载 500 根 1h K 线（约 20 天历史），运行期实时追加，上限 600 根。

**步骤二：特征提取（`RegimeFeatureSource`）**  
从最近 30+ 根 1h K 线提取以下时序特征：

| 特征 | 计算定义 | 说明 |
|------|---------|------|
| `atr_pct` | 最近14根bar的 `max(H-L, |H-prev_C|, |L-prev_C|)` 均值 ÷ 当前close | 每根K线内部振幅，非全天价格区间 |
| `bb_width` | (布林上轨 - 布林下轨) ÷ 布林中轨，取最近20根bar计算 | |
| `ret_roll_std_20` | 最近20根bar的对数收益率标准差 | |
| `adx` | 平均方向指数，取最近14根bar | |
| `rsi_14` | 相对强弱指数 | |

**步骤三：评分（`HybridRegimeScorer`）**  
基于阈值规则混合评分：

```
高波动得分 = atr_pct / atr_pct_high + bb_width / bb_width_high + ret_std / ret_std_high
             （三项指标各自归一化后加总，任一超阈值即大幅拉高 high_vol 分）

阈值参考：
  atr_pct_high = 0.025（2.5%）
  bb_width_high = 0.060（6.0%）
  ret_std_high  = 0.018（1.8%）
```

**步骤四：概率归一化**  
四类别（bull/bear/sideways/high_vol）softmax 归一化，含 `prob_floor = 0.05` 保底。  
`high_vol` 得分高时，通过 `vol_suppression` 机制压制其他三类的趋势/动量分，使其被推到 floor（约 4.3%）。

**步骤五：置信度**  
```python
confidence = top1_prob - top2_prob
```
即第一名与第二名概率之差，差距越大 = 置信度越高。

### 1.3 "状态稳定" 的真实含义

> **正确理解**：最近 5 根 1h K 线的 `dominant_regime` 是否完全一致（全是 high_vol / 全是 bull 等）。  
> **误读警告**："稳定" ≠ "市场平静"。高波动市场连续 5 根 bar 都判定为 high_vol，该字段依然显示"稳定"。

### 1.4 为什么今天价格区间窄仍显示"高波动"

`atr_pct` 计算的是过去 14 根 1h bar **各自内部**的高低价振幅均值，不是"今日全天最高价 - 最低价"。  
rolling 窗口约 14~20 小时，若前几天 SOL 有过较大波动，即使今天相对平静，  
14~20 bar 的滚动统计仍可能保持在阈值以上，`high_vol` 判定因此维持。

---

## 2. 市场状态概率分布（四色概率条）

| 显示名 | 代码字段 | 说明 |
|--------|---------|------|
| 牛市 % | `RegimeState.bull_prob` | softmax 归一化后的概率，含 floor=5% |
| 熊市 % | `RegimeState.bear_prob` | 同上 |
| 震荡 % | `RegimeState.sideways_prob` | 同上 |
| 高波动 % | `RegimeState.high_vol_prob` | 同上 |

> **注意**：当 high_vol 概率接近 87% 时，其余三类通常各约 4.3%。  
> 这是 `prob_floor=0.05` 保底 + softmax 归一化的数学必然结果，不代表"轻微看涨"或"轻微看跌"。  
> 理解方式：系统的真实判断是"几乎确定高波动"，其余三类概率只是数值上的最低下限。

---

## 3. 门控动作（GatingEngine）

### 3.1 合法动作枚举

| 动作 | 含义 |
|------|------|
| `ALLOW` | 允许全部方向信号通过 |
| `REDUCE` | 允许信号通过，但权重乘以 `reduce_factor`（默认 0.5） |
| `BLOCK_BUY` | 阻断所有 BUY 方向信号，SELL/HOLD 可通过 |
| `BLOCK_ALL` | 阻断全部信号 |

### 3.2 触发条件（按优先级）

```
1. dominant_regime == "unknown"           → REDUCE（或 BLOCK_ALL，取决于配置）
2. confidence < 0.05（极低置信度）        → BLOCK_ALL
3. confidence < 0.20（低置信度）          → REDUCE
4. high_vol 且 confidence >= 0.5          → BLOCK_BUY
5. is_regime_stable == False              → REDUCE
```

### 3.3 "门控动作 = 未知" 的含义

不是 GatingEngine 的合法输出。代表快照侧拿不到最新 `OrchestrationDecision`（通常是交易引擎还未完成第一轮循环）。

---

## 4. 策略环境亲和度权重（非表现权重）

### 4.1 这些权重代表什么

> **关键理解**：权重 = 当前 regime 下该策略的"环境亲和度"权重，**不是历史盈利贡献**，**不是胜率**。  
> 两个策略的权重比（如 45:55）反映的是"在当前市场状态下，哪个策略更适合当前环境"。

### 4.2 计算链

```python
# 1. 亲和度查表（configs/system.yaml 或 ScorerConfig 中配置）
#    每个 regime 对每个策略类型预定义一个亲和度分数 (0.0 ~ 1.0)
#    示例（high_vol 场景）：
#      momentum_strategy  → affinity = 0.50
#      ma_cross_strategy  → affinity = 0.40

# 2. 权重映射（线性）
#    weight = min_weight + affinity × (max_weight - min_weight)
#    其中 min_weight=0.1, max_weight=2.0
#    → momentum: 0.1 + 0.5 × 1.9 = 1.05
#    → ma_cross: 0.1 + 0.4 × 1.9 = 0.86

# 3. 归一化
#    momentum_pct = 1.05 / (1.05 + 0.86) ≈ 55%
#    ma_cross_pct = 0.86 / (1.05 + 0.86) ≈ 45%
```

### 4.3 表现折扣

若 `PerformanceStore` 中记录的该策略 `avg_signal_confidence < 0.10`，  
权重额外乘以 `perf_discount_factor`（默认 0.7），并在 `阻断原因` 中记录。

> **注意**：`avg_signal_confidence` 是策略发出信号时的置信度字段均值，  
> 不是胜率，不是盈利率。当前 legacy 策略（规则策略）在无持仓时发出 HOLD 信号，  
> HOLD 信号的置信度固定为 0.0，导致 avg_signal_confidence 长期为 0.0。  
> 因此表现折扣文字中的 `avg_conf=0.000` 是"信号强度统计为0"，  
> **不代表"该策略历史上从未盈利"或"胜率为0"**。

---

## 5. 阻断原因（Block Reasons）

### 5.1 来源层级

阻断原因来自两个独立层次，不要混淆：

**层次一：GatingEngine 环境门控**（显示在 `block_reasons` 中）
- `high_vol_block_buy` — 高波动环境禁止做多
- `regime_unknown` — 市场状态未知
- `regime_very_low_confidence` — 置信度极低，全面阻断
- `regime_low_confidence` — 置信度偏低，降低权重
- `regime_unstable` — 最近5根bar状态不一致

**层次二：权重折扣（不是真正的"阻断"）**（也出现在 `block_reasons` 中）
- `{策略ID} 历史信号置信度偏低(avg_signal_conf=X.XXX, 已折扣权重)` — 权重打折，但信号未被完全拦截

**层次三：风控熔断**（不在此页面显示，见系统总览页）
- `risk_manager` 拒单、`kill_switch`、回撤熔断等

### 5.2 "无阻断原因" 不等于 "无任何风控动作"

此处显示"无阻断原因"仅代表 GatingEngine 本轮未触发 block_reason 规则。  
风控层（risk_manager）的拒单信息需在系统总览页的"最近拒单"中查看。

---

## 6. 已选策略结果（信号输出）

### 6.1 字段含义

| 字段 | 含义 |
|------|------|
| 策略 | 策略 ID |
| 标的 | 交易品种 |
| 动作 | BUY / SELL / HOLD |
| 信号强度 | 该策略发出此信号时的置信度字段值（不是预测胜率） |

### 6.2 "置信度/信号强度 = 0.0%" 的含义

> 规则策略（legacy adapter）在当前 bar **无持仓时默认发出 HOLD 信号**，  
> 置信度字段固定为 `0.0`（不是"模型认为胜率0%"，而是"这不是一个主动信号"的占位值）。  
> 只有策略发出真实的 BUY/SELL 信号时，置信度才会被设为 1.0。

---

## 7. 持续学习器（Continuous Learner）

### 7.1 当前状态为空的原因

此模块需要磁盘上存在 ML 模型文件（位于 `models/` 目录）。  
若该目录为空，系统会显示"学习器数量：0 / 版本：无 / 模型路径：无"，  
这是**正常初始状态**，不是程序错误。

### 7.2 完整触发链

```
训练触发条件（三选一）：
  1. 达到 retrain_every_n_bars 设定的间隔（默认每500根bar）
  2. 模型性能退化（F1 < 上次训练 F1 的 80%）
  3. 特征分布漂移（KS统计量 > 0.3）

训练过程：
  WalkForwardTrainer → XGBoost/LightGBM/RandomForest 三模型集成
  → Optuna 超参数搜索
  → 验证集评估 + 阈值优化
  → 通过后热替换线上模型（_hot_swap_model）
```

### 7.3 AI 分析（Gemini 集成）

优先读取环境变量 `GEMINI_API_KEY`，并兼容旧的 `GOOGLE_API_KEY`。  
每隔 N 个主循环周期，取最近 10 根 K 线的收盘价发送给 Gemini API，  
返回市场情绪分析文字。未配置时固定显示 `Waiting for AI analysis...`。

---

## 8. 快速对照表：字段真实含义

| 显示名 | 真实含义 | 常见误读 |
|--------|---------|---------|
| 置信度 82.6% | top1_prob - top2_prob | ≠ 预测准确率，≠ 胜率 |
| 状态稳定：是 | 最近5根bar的dominant_regime一致 | ≠ 市场平静或低波动 |
| 策略权重 45/55 | regime 环境亲和度权重归一化 | ≠ 历史盈利贡献，≠ 胜率权重 |
| 信号强度 0.0% | legacy策略HOLD信号的占位置信度 | ≠ 胜率0%，≠ 策略无效 |
| avg_signal_conf=0.000 | HOLD信号置信度字段均值为0 | ≠ 历史胜率，≠ 盈利能力 |
| 持续学习器：0个 | models/目录无ML模型文件 | ≠ 程序错误 |
| 无阻断原因 | GatingEngine本轮未触发 | ≠ 无任何风控动作 |
| 高波动概率87% | softmax归一化后的类别概率 | ≠ "下一根K线会波动87%" |
| 牛市/熊市/震荡各4.3% | prob_floor保底值 | ≠ "轻微看涨/看跌" |
