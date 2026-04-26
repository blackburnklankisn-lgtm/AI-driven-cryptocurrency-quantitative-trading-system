# 数据融合工作区测试与验证报告（2026-04-26）

## 1. 结论摘要

本次对“数据融合”工作区做了代码级追踪 + 运行态接口验证，结论如下：

1. 你看到的 `subscription_manager.health = unavailable` 是后端字段接线错误导致，不是订阅管理器本身不可用。
2. 你看到的 `orderbook/trade/onchain/sentiment = unknown` 主要是接口层没有接到真实健康数据，默认回退值被前端原样展示。
3. “新鲜度汇总”的 `status/field_count` 在原实现里是简化占位逻辑，只看 `latest_prices`，并非完整多源 freshness 评估。
4. “最新价格只更新一次”在当前代码设计下，理论更新节奏是：
   - 主循环 K 线路径：约 60 秒一次
   - API 侧 ticker 刷新 worker：约 5 秒一次（best effort）
   - 前端 data-health ws 推送：约 3 秒一次
5. 我已将可修复的接线问题修复并复验，现已能返回真实订阅/OrderBook/Trade/链上/情绪健康信息。

## 2. 你问的每个问题逐条解释

### 2.1 新鲜度汇总参数是什么意思？原理和作用是什么？

原页面显示位置：
- [apps/desktop-client/src/pages/DataFusionPage.tsx](apps/desktop-client/src/pages/DataFusionPage.tsx#L33)

原后端字段构造位置：
- [apps/api/server.py](apps/api/server.py#L968)

#### 参数含义

1. `status`
   - 表示当前“数据融合视角下”的整体新鲜度状态。
   - 常见值：`fresh` / `partial` / `stale`。

2. `field_count`
   - 当前 `latest_prices` 中有多少个交易对价格（如 BTC/ETH/SOL 则为 3）。

3. `source_count`（本次修复新增）
   - 融合视角下统计的核心来源个数（价格、订阅、orderbook、trade、onchain、sentiment 中的核心源计数）。

#### 原理/机制

- 原实现只用 `latest_prices` 是否为空判断 freshness，逻辑偏简化。
- 修复后：
  - 会综合多个健康源状态来决定 freshness summary。
  - 当任一关键源出现 `stale/degraded/missing/error` 时，`stale_fields` 会列出具体字段。

#### 在项目中的作用

- 作为“可视化运行态体检入口”，帮助快速判断是价格源问题、订阅问题、还是低频外部源问题。
- 未来可直接作为 KillSwitch 或自动降级策略的观测输入（目前主要用于监控展示）。

### 2.2 为什么订阅管理器显示 unavailable？订阅管理器作用和机制是什么？

订阅管理器实现：
- [modules/data/realtime/subscription_manager.py](modules/data/realtime/subscription_manager.py#L92)

其在 Trader 的真实字段：
- [apps/trader/main.py](apps/trader/main.py#L552)

#### 原因

- API 在 data_fusion 快照里读取的是 `_subscription_manager`。
- 但运行时实际字段是 `_phase3_subscription_manager`。
- 结果：API 取不到对象，走默认值 `{"health":"unavailable"}`。

原问题代码（已修复）：
- [apps/api/server.py](apps/api/server.py#L833)

#### 订阅管理器作用

1. 管理 WS 行情连接生命周期（连接/断开/重连）。
2. 统一订阅交易对列表。
3. 做心跳健康检查，发现断连后指数退避重连。
4. 路由 depth/trade 数据到缓存注册器（DepthCacheRegistry/TradeCacheRegistry）。

#### 机制

- 健康状态枚举：`healthy/degraded/stopped`。
- 后台线程按配置间隔做心跳检查。
- 心跳超时或状态异常会触发重连，并更新健康状态。
- 可通过 `diagnostics()` 输出 `health/exchange/subscribed_symbols/reconnect_count/ws_state`。

### 2.3 为什么数据源健康矩阵都是 unknown？是不是取不到数据？源数据在哪里？什么时候更新？

页面显示位置：
- [apps/desktop-client/src/pages/DataFusionPage.tsx](apps/desktop-client/src/pages/DataFusionPage.tsx#L43)

原后端回退来源：
- [apps/api/server.py](apps/api/server.py#L963)

#### 原因

- 原实现读取了 Trader 上并不存在的属性：
  - `_orderbook_health`
  - `_trade_feed_health`
  - `_onchain_health`
  - `_sentiment_health`
- 这些属性不存在时统一回退 `{"status":"unknown"}`，前端自然显示“未知”。
- 不是“前端没拿到接口”，而是“接口给了占位值”。

#### 源数据真实来源

1. OrderBook：`_phase3_depth_registry.diagnostics()`
   - 源实现：[modules/data/realtime/depth_cache.py](modules/data/realtime/depth_cache.py#L433)
2. Trade：`_phase3_trade_registry.diagnostics()`
   - 源实现：[modules/data/realtime/trade_cache.py](modules/data/realtime/trade_cache.py#L328)
3. Onchain：`_onchain_collector` + `cache.evaluate_freshness()`
   - 源实现：[modules/data/onchain/collector.py](modules/data/onchain/collector.py#L229)
   - freshness：[modules/data/onchain/cache.py](modules/data/onchain/cache.py#L174)
4. Sentiment：`_sentiment_collector` + `cache.evaluate_freshness()`
   - 源实现：[modules/data/sentiment/collector.py](modules/data/sentiment/collector.py#L194)
   - freshness：[modules/data/sentiment/cache.py](modules/data/sentiment/cache.py#L177)

#### 更新场景

1. 实时行情流正常时：depth/trade 诊断会持续变化。
2. 外部低频源采集或读取缓存时：onchain/sentiment freshness 会随采集时间和 TTL 变化。
3. API 推送周期：data-health ws 约每 3 秒推送一次。

### 2.4 过期字段什么时候会出现？原理是什么？作用是什么？

字段构造位置：
- [apps/api/server.py](apps/api/server.py#L984)

#### 出现场景

1. 最新价格为空（如行情未初始化）会出现 `latest_prices`。
2. 某个健康源状态是 `stale/degraded/missing/error/failed` 时，会把该源字段名加入 `stale_fields`。

#### 原理

- 本质是一个“异常字段列表”，把 freshness/health 失败的源名显式列出来。
- 前端只负责渲染，不做推断。

#### 作用

- 排障更快：一眼知道是“哪个源有问题”，避免只看到笼统“异常”。
- 可以作为后续自动化告警和降级策略的触发条件。

### 2.5 为什么最新价格只更新一次就不变？理论多久更新一次？15s 还是 60s？

相关实现：
- 主循环轮询间隔：[apps/trader/main.py](apps/trader/main.py#L266)
- 主循环 sleep： [apps/trader/main.py](apps/trader/main.py#L1475)
- API ticker worker（5 秒）：[apps/api/server.py](apps/api/server.py#L218)
- ws 推送周期（3 秒）：[apps/api/server.py](apps/api/server.py#L189)

#### 结论

1. 理论不是 15 秒，也不是只能 60 秒。
2. 当前设计是“双通道更新”：
   - 主循环约 60 秒更新（K线路径）
   - API 侧补充每 5 秒尝试 `fetch_ticker` 更新 `_latest_prices`
3. 前端通过 data-health ws 约 3 秒收到一次快照。

#### 你体感“只更新一次”的常见原因

1. 接口层之前大量占位值，造成“看起来不动”。
2. ticker worker 每 5 秒是 best effort：如果 `fetch_ticker` 返回相同价格或请求失败，显示上可能暂时不变。
3. 若 WS 断开，页面会回退到静态 snapshot，视觉上像“冻结”。

## 3. 本次已实施修复

修复文件：
- [apps/api/server.py](apps/api/server.py#L833)

修复内容：

1. `subscription_manager` 改为优先读取 `_phase3_subscription_manager`，兼容旧字段。
2. `orderbook_health` 改为基于 DepthRegistry 诊断聚合真实状态。
3. `trade_feed_health` 改为基于 TradeRegistry 诊断聚合真实状态。
4. `onchain_health` / `sentiment_health` 改为 collector+cache freshness 真实评估。
5. `stale_fields` 改为按各源状态自动收集，不再只看 latest_prices。
6. `freshness_summary` 增强，新增 `source_count`。
7. probe symbol 修正为实际交易符号（如 `BTC/USDT`），避免误判 missing。

## 4. 修复后实测结果

实测接口：
- `/api/v2/dashboard/data-fusion`

修复后返回结果特征：

1. `subscription_manager.health` 从 `unavailable` 变为 `healthy`，且包含 `exchange/ws_state/reconnect_count/subscribed_symbols`。
2. `orderbook_health` / `trade_feed_health` 不再是 `unknown`，可看到每个 symbol 的诊断细节。
3. `onchain_health` / `sentiment_health` 可返回 freshness 结果（如 `fresh`）与 `lag_sec/ttl_sec`。
4. `freshness_summary` 与 `stale_fields` 会随源状态联动变化。

## 5. 对“是否能解决当前未实现功能”的结论

可以，且已完成主要可落地项：

1. 订阅管理器 unavailable：已解决。
2. 健康矩阵 unknown 占位：已解决为真实诊断。
3. 过期字段展示逻辑过简：已增强。
4. 新鲜度汇总过简：已增强为多源状态汇总。

仍可继续优化（可选下一步）：

1. 将 freshness 统一切到 `FreshnessEvaluator` 的完整多字段评估模型，避免接口层自行聚合规则。
2. 对 latest_prices 增加 `updated_at`/`age_sec` 字段，让“价格没变”与“价格没更新”可区分。
3. 在前端 DataFusion 页显式展示 `ws connected` 与最近一帧 `generated_at` 延迟。
