# AI 驱动加密货币现货量化交易系统

> 机构级 · 模块化 · 可审计 · 可回测 · 可实盘

## 核心特性

- ✅ 严格分层的六层事件驱动架构
- ✅ 回测与实盘共用同一代码路径
- ✅ 风控强制约束高于所有策略和AI信号
- ✅ 所有私钥/API Key 通过环境变量管理，不得硬编码
- ✅ 禁止未来函数、数据泄露与幸存者偏差
- ✅ CCXT 统一交易所接入 (Binance/OKX/Coinbase 等)

## 架构分层

```
数据层 → Alpha引擎层 → 聚合层 → 策略组合层 → 执行层 → 分析反馈层
```

## 技术栈

| 类别 | 工具 |
|------|------|
| 语言 | Python 3.11+ |
| 数据计算 | pandas, polars, numpy, numba |
| 机器学习 | scikit-learn, xgboost, lightgbm |
| 交易所接入 | CCXT |
| 存储 | PostgreSQL + TimescaleDB, Parquet |
| 消息中间件 | Redis Pub/Sub |
| 监控 | Prometheus + Grafana |
| 测试 | pytest, pytest-asyncio |
| 代码质量 | ruff, mypy, pre-commit |
| 容器化 | Docker + docker-compose |

## 快速开始

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置密钥
cp .env.example .env
# 编辑 .env，填入你的 API Key

# 3. 启动基础设施
docker-compose -f docker/docker-compose.yml up -d

# 4. 初始化数据库
python scripts/init_db.py

# 5. 运行测试
pytest tests/

# 6. 模拟盘运行
TRADING_MODE=paper python -m apps.trader.main
```

## Phase 3 Realtime Feed

- `phase3.realtime_feed.provider` 现在默认是 `htx`，paper 模式会优先消费 HTX public market feed。
- 如需在离线环境或 CI 中保持稳定，可将 [configs/system.yaml](configs/system.yaml) 中的 provider 切回 `mock`。
- 可先用下面的 smoke 命令验证 realtime feed：

```bash
python scripts/verify_phase3_realtime_feed.py --provider htx --exchange htx --symbol BTC/USDT --timeout 20
```

- 若只想做本地离线验证，可改用：

```bash
python scripts/verify_phase3_realtime_feed.py --provider mock --exchange mock --symbol BTC/USDT --timeout 1
```

## 目录结构

```
.
├── apps/            # 可独立启动的服务入口
│   ├── trader/      # 实盘主控
│   ├── backtest/    # 回测引擎
│   ├── research/    # 研究辅助
│   └── data/        # 数据采集节点
├── core/            # 基础设施：事件总线/配置/日志/异常
├── modules/         # 业务逻辑
│   ├── alpha/       # 信号与特征工程
│   ├── ensemble/    # 聚合与状态识别
│   ├── strategy/    # 策略与组合
│   ├── risk/        # 风控与资金管理
│   ├── execution/   # 订单执行
│   └── analysis/    # 归因分析与审计
├── configs/         # YAML 全局配置
├── docker/          # 容器编排文件
├── notebooks/       # 研究用 Jupyter 笔记
├── scripts/         # 运维与初始化脚本
├── storage/         # 本地数据 (gitignored)
├── logs/            # 运行日志 (gitignored)
└── tests/           # pytest 测试用例
```

## 安全须知

- `.env` 文件已在 `.gitignore` 中，**绝不可提交到代码仓库**
- 生产环境建议使用 HashiCorp Vault 或 AWS Secrets Manager
- 禁止在代码中硬编码任何密钥

## 开发规范

- 提交前执行 `ruff check . && mypy .`
- 所有新模块须附带 `tests/` 中的对应测试文件
- 策略上线前必须经过：回测 → walk-forward → 模拟盘 → 故障注入 四个阶段
