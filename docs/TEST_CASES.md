# AI 驱动加密货币现货量化交易系统 - 全面系统级测试用例库 (System Test Cases)

> **文档状态**: V1.0 (创建于阶段六完成后)
> **测试目标**: 涵盖从基础设施、数据获取、多因子策略、ML 持续学习到资金管理和实盘执行的全部 6 个阶段。验证“无未来函数泄露”、“系统健壮性”、“交易安全性”及“可审计性”。

---

## 阶段一：核心基础设施层 (Core Infrastructure)

### 1.1 事件总线 (EventBus)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-INF-001 | 路由分发 | 注册不同优先级（HIGH/NORMAL/LOW）的事件监听器，发布三个事件 | 监听器严格按照 `HIGH -> NORMAL -> LOW` 顺序被触发，且主题 (topic) 匹配正确。 |
| TC-INF-002 | 异常隔离 | 在某个普通监听器中抛出 `Exception`（模拟策略异常） | EventBus 捕获异常，将异常记录到日志中，不阻塞后续监听器，系统继续平稳运行。 |
| TC-INF-003 | 线程安全 | 多线程并发向 EventBus 发送不同的市场和执行事件 | 队列不会发生死锁或数据丢失，内部 `queue.Queue` 阻塞保证了消息送达。 |

### 1.2 配置中心 (ConfigManager)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-INF-004 | 环境变量覆盖 | 在环境配置中注入 `BINANCE_API_KEY=testkey`，并在 `.yaml` 中缺失此项 | 内存配置字典能成功合并环境变量，读取到正确 Key，不依赖硬编码。 |
| TC-INF-005 | 类型校验 | 配置文件提供字符串类型的超时时间，但系统依赖 `int` | 配置校验抛出类型错误异常，拒绝启动（Pydantic 校验生效）。 |

### 1.3 日志与审计 (Logger)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-INF-006 | 脱敏与防泄漏 | 尝试在策略层 `log.info("API Key: " + secret)` | Logger 审查器能在终端输出前屏蔽掉敏感密钥字符（如果存在正则表达式过滤层）或系统约定策略层无权访问密钥。 |
| TC-INF-007 | 审计留痕 | 触发一次关键调用 `audit_log("ORDER_PLACED", ...)` | 审计日志单独存储在 `logs/audit.log`，且结构化为 JSON 格式以便归档追踪。 |

---

## 阶段二：数据层 (Data Pipeline)

### 2.1 下载器与解析 (Downloader)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-DAT-001 | API 限制回退 | 短时间高频请求 CCXT 分页历史数据，触发 429 报错 | 下载器能捕获抛出并执行退避重试 (Exponential Backoff)，随后成功拉取数据。 |
| TC-DAT-002 | 边界解析 | 拉取的最后一条 K 线是未收线 (is_closed=False) 的数据 | 不将其计入历史 Parquet 数据中，保证本地数据 100% 收线。 |

### 2.2 数据校验 (Validator)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-DAT-003 | 缺失连续性 | 提供一个缺失了一根 1h 数据的 DataFrame | Validator 的 `check_continuity` 抛出 `DataValidationError`。 |
| TC-DAT-004 | 负价格与乱序 | 模拟伪造数据，`high` < `low`，或者时间戳乱序 | 校验器报警抛弃异常段，不允许将错误数据入库。 |

### 2.3 数据持久化 (Parquet Storage)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-DAT-005 | 高效读写 | 存入并读取 1,000,000 条 K 线数据 | 存取耗时 < 1秒，数据读取时列格式（Decimal等）与原始保持绝对一致。 |

---

## 阶段三：Alpha 策略与风控层 (Alpha & Risk)

### 3.1 规则策略引擎 (BaseAlpha & MACross)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-STR-001 | 防未来函数 | 向策略推送 `KLineEvent`，随后私自修改内存中的未到达数据 | 特征引擎只对 `event.timestamp` 及之前的 K 线进行计算 `MA`，后续突变数据完全不影响当期信号生成。 |
| TC-STR-002 | 冷却期抑制 | 条件满足产生极其高频的切换信号 | `OrderRequestEvent` 触发后进入冷却，策略在接下来 N 根 K 线内无视交叉信号（cooling bars）。 |

### 3.2 资金管理与仓位 (Position Sizer)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-POS-001 | 现金流校验 | 获取 0 现金，产生通过策略的 `BUY` 信号 | PositionSizer 发现现金不足，将下单量降级至 0，拒绝产生真实请求。 |
| TC-POS-002 | 基础货币限额 | 生成下单额度超过单笔限额 40% 的资产购买请求 | Request 自动截断到 40% （Max Weight），绝不超出限额。 |

### 3.3 熔断器机制 (Risk Manager - 核心安全)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-RSK-001 | 连亏熔断 | 在模拟执行中连续向 EventBus 提供 5 个亏损的 `OrderFilledEvent` | 触发最大连亏 `check() -> False` 截断所有后续 `buy` 信号，系统进入 `CIRCUIT_BROKEN`，只允许平仓操作。 |
| TC-RSK-002 | 单日亏损熔断 | 单日内累计 PNL 下跌超过预设 `daily_loss_limit` | 与连亏表现一致，全盘拒绝新开单。 |
| TC-RSK-003 | 手动解封 | 熔断状态下重启软件或跨日 | 熔断状态不解除（安全降级），直到调用指定接口/管理员介入清理恢复为止。 |

---

## 阶段四：实盘执行与监控 (Execution & Monitoring)

### 4.1 交易所网关 (CCXT Gateway)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-EXE-001 | Paper/Live区分 | 设置 `TRADING_MODE=paper` 然后挂单买入 | 仅本地记录日志 "Simulated Buy"，不向交易所发送 API HTTPS 请求。 |
| TC-EXE-002 | 异常映射 | CCXT 抛出 `NetworkError` 或 `RateLimitExceeded` | 统一封装映射为业务内定义的 `ExecutionError`，被 OrderManager 优雅捕捉并标记超时/重试。 |

### 4.2 订单生命周期管理 (Order Manager)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-EXE-003 | 状态机流转 | 成功提交一笔订单，并接连收取到 2 次 `PARTIAL_FILLED`，最后完成 `FILLED` | 状态由 `PENDING -> SUBMITTED -> PARTIAL_FILLED -> FILLED` 严密流转，仓位同步缓慢增加，不出现账外差额。 |
| TC-EXE-004 | 超时自动撤单 | 挂单后长时间无成交 | OrderManager 内部轮询检查到超时，主动向 CCXT 发送 Cancel 限价单，并将未成交释放回可用资金。 |

### 4.3 基础设施与监控 (Monitoring)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-MON-001 | 优雅降级 | 未安装/未启动 Prometheus `prometheus-client`，运行程序 | 系统发现异常，使用 NullMetrics/No-op 类绕过所有监控代码，程序主体正常运行。 |
| TC-MON-002 | 熔断暴露 | 触发一次策略熔断 | Prometheus 中 `risk_circuit_breakers_active` 取值由 0 变 1，Grafana 红色警戒图表激活。 |

---

## 阶段五：ML Alpha 机器学习 (ML Engine)

### 5.1 数据准备与标签生成 (Feature & Labeler)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-ML-001 | 负向偏移阻断 | 运行 `MLFeatureBuilder` 并执行 `shift(-1)` 来包含未来数据 | 代码约束和单元测试警告拦截，确保特征矩阵中的 `shift` 全部 > 0，保障历史单向。 |
| TC-ML-002 | 标签边界处理 | 运行 `ReturnLabeler` 并在数据集的最后 5 行尝试预测 `forward_bars=5` 标签 | 最后 5 行强制设为 `NaN`（截断 Embargo期），并在使用前自动 `dropna()`，这 5 行不参与任何训练。 |
| TC-ML-003 | Embargo 时序检查 | `check_no_leak` 测试中让训练集和测试集索引距离小于 5 格 | 抛出 `FutureLookAheadError` 终止流程，保证训练测试集的时序隔离。 |

### 5.2 验证训练与推理 (Walk-Forward Trainer & Predictor)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-ML-004 | 稳健基线降级 | 未按要求安装 `LightGBM`，但配置选用它 | `SignalModel` 退化降级加载 `RandomForest` 继续安全训练，并在日志输出 Fallback 警告。 |
| TC-ML-005 | 单点流式推理 | 提供一根最新的 `KLineEvent` 给已经加载好模型的 `MLPredictor` | 预热 `Deque` 数据后，仅对矩阵执行 `iloc[-1]` 的模型推演，并且 `buy_proba` 小于阈值时不输出单。 |

---

## 阶段六：组合管理与持续学习 (Portfolio & Continuous Learning)

### 6.1 多资产权重分配 (PortfolioAllocator)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-PTF-001 | 最小方差求解器 | 提供方差极大差异化的两种资产计算 `MINIMUM_VARIANCE` | 大方差资产分得极小权重。若矩阵无解由于共线性，则优雅退化至风险平价(`Risk Parity`)。 |
| TC-PTF-002 | 权重约束截断 | 产生 90% 集中配置但 `weight_cap` = 0.62 | 配置被截断至 0.62，其余资金分配给其它标的，并且所有最终加和严格等于 1.0。 |

### 6.2 组合再平衡与执行顺序 (PortfolioRebalancer)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-PTF-003 | 先卖后买死锁解除 | 进行资产 A 过配、资产 B 欠配的再平衡 | 生成两笔订单队列：**必定先生成卖单(Sell A)再生成买单(Buy B)**，以此保护持仓无足够现钞可供透支。 |
| TC-PTF-004 | 尘埃碎片过滤 | 漂移触发生成一个只相差 \$2 USDC 的再平衡购买指令 | 低于 `min_trade_notional`（如\$10），忽略该细微调整，节省佣金。 |

### 6.3 持续学习监控调度 (ContinuousLearner)
| 用例编号 | 场景分类 | 测试动作描述 | 预期结果 |
|---|---|---|---|
| TC-CLN-001 | 概念漂移 KS 检测 | 向近期的流缓存输入一波均值及方差极大（3倍标准差）的合成错误特征 | 特征 KS 检验超过设定比例 >30% (p<`drift_significance`)，触发重训练原因记录为 `concept_drift`。 |
| TC-CLN-002 | AB无缝切换 | 模型触发定时重训 (500 bars) 但新版 OOS_Acc 表现相比当前更差 (< 1%净改善) | 选择暂扣：新模型不会部署 (`is_active=False`)，旧版模型继续保留活跃权限，确保“没有变得更好就不要瞎换”。 |
| TC-CLN-003 | 安全并发 | 重训练期间再次产生新漂移告警尝试并行触发重训任务 | `_is_retraining` 锁拦截操作，确保同一时间只消耗一次 CPU 密集任务。 |

---

### [系统全链路级 冒烟测试 (E2E Smoke Testing)]
| 用例编号 | 前提条件 | 测试步骤 -> 期望链条行为验证 |
|---|---|---|
| **E2E-SMOKE-1** | `TRADING_MODE=paper`，Docker栈启动 | 1. `main.py` 启动；<br>2. K线拉取；<br>3. `MLPredictor` 预测产生买入；<br>4. `RiskManager` 审计批准买入；<br>5. `OrderManager` 分发至 `CCXTGateway`；<br>6. 模拟填充，发送持仓变更回 `EventBus`；<br>7. `SystemMetrics` 指标抓取被 Grafana 显示。<br>👉 **整条链路任何一处报错，核心进程立即安全中断** |
