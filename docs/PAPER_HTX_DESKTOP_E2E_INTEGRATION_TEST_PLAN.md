# Paper 模拟盘（HTX）前后端全链路联调方案

更新时间：2026-04-25

## 1. 目标与范围

本方案用于验证以下端到端能力在真实联网环境中可用：

1. 后端以 `paper` 模式运行，并从 HTX 获取实时 ticker 与 K 线数据。
2. REST 接口可持续返回最新数据：
   - `GET /api/v1/status`
   - `GET /api/v1/klines?symbol=BTC/USDT`
   - `GET /api/v2/dashboard/snapshot`
3. WebSocket 实时通道可收到增量推送：
   - `WS /api/v1/ws/status`
   - `WS /api/v1/ws/logs`
4. 前端（dev 或 electron）可消费上述数据并在 UI 页面显示（Overview K 线、指标卡、Data Fusion、Execution & Audit）。
5. 构建与打包可通过（`npm run build`）。

## 2. 前置条件

1. 机器可访问互联网与 HTX 公共接口。
2. 项目依赖已安装：Python + Node/npm。
3. 根目录存在 `.env`，并至少包含：
   - `TRADING_MODE=paper`
   - `EXCHANGE_ID=htx`
   - `HTX_API_KEY`
   - `HTX_SECRET`
4. 端口未被占用：
   - `8000`（FastAPI）
   - `5173`（Vite dev）

## 3. 测试策略

采用“先后端数据真实性、再链路连通性、再前端显示一致性”的三层策略。

1. 数据源真实性：直接验证 HTX feed 与 K 线是否更新。
2. 服务链路正确性：验证 API 与 WS 的响应结构、时序和更新。
3. UI 一致性：验证页面中的关键展示字段与 API 返回一致。

## 4. 详细执行步骤

### 阶段 A：环境与配置核验

1. 校验 `.env` 是否存在且关键变量非空（不打印密钥值）。
2. 校验 `configs/system.yaml` 默认 provider/exchange 设置。
3. 执行 HTX realtime feed 烟雾脚本：
   - `python scripts/verify_phase3_realtime_feed.py --provider htx --exchange htx --symbol BTC/USDT --timeout 20`

验收标准：脚本返回成功并出现有效价格更新。

### 阶段 B：后端（paper）启动与 REST 联调

1. 启动后端主控：
   - `TRADING_MODE=paper python -m apps.trader.main`
2. 轮询以下接口，至少 3 轮：
   - `GET /api/v1/health`
   - `GET /api/v1/status`
   - `GET /api/v1/klines?symbol=BTC/USDT`
   - `GET /api/v2/dashboard/snapshot`
3. 校验字段：
   - `status.mode == paper`
   - `status.exchange == htx`
   - `klines` 数组长度 > 0，最后一根 close 为数值
   - `snapshot.overview.feed_health.health` 存在
   - `snapshot.data_fusion.latest_prices.BTC/USDT`（或同义交易对）存在且数值变化

验收标准：所有接口连续可用，关键字段完整，价格/K线存在并更新。

### 阶段 C：WS 联调

1. 连接 `WS /api/v1/ws/status`，等待 15~30s，至少收到 1 条状态推送。
2. 连接 `WS /api/v1/ws/logs`，等待 15~30s，至少收到 1 条日志。
3. 可选连接 v2 领域 WS（dashboard/risk/execution），验证 JSON 可解析。

验收标准：连接成功、消息可解析、无持续断连。

### 阶段 D：前端 dev 联调

1. 启动前端：`apps/desktop-client` 下运行 `npm run dev`。
2. 打开 `http://127.0.0.1:5173`，检查：
   - Overview 页 K 线图有数据。
   - Daily PnL、Drawdown、Risk 等指标卡有值。
   - Data Fusion 页 latest prices 表格有实时价格。
   - Execution & Audit 页：订单/持仓/审计日志流存在。
3. 对比接口与 UI 的关键字段一致性（至少抽样 3 项）。

验收标准：页面无白屏/报错，关键卡片与接口数据一致。

### 阶段 E：Electron 联调（可选但推荐）

1. 运行 `npm run dev`（含 electron:dev）或运行打包产物。
2. 验证与 dev 模式相同的关键页面展示。
3. 执行 `npm run build`，确认打包成功。

验收标准：Electron 模式可启动并显示完整数据；安装包构建成功。

## 5. 风险与回退

1. 若 HTX 网络不可达：记录失败并切到 `mock` 仅做链路验证（不算通过本任务）。
2. 若 `win-unpacked` 被占用：先结束 `AI Quant Trader/backend_trader`，再清理目录后重试。
3. 若端口冲突：释放 8000/5173 后重启。

## 6. 最终输出物

1. 本联调方案文档。
2. 实际执行日志（命令与关键输出）。
3. 结果结论：通过/失败 + 失败原因与修复建议。

## 7. 通过判定

同时满足以下条件才判定通过：

1. HTX 实时数据可获取（ticker + K线）。
2. 后端 REST/WS 链路全通。
3. 前端页面关键数据可显示且与 API 一致。
4. Electron 构建成功。
