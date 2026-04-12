-- TimescaleDB 初始化脚本
-- 创建时序数据表并启用时序压缩

-- K 线历史数据（可选，作为 Parquet 的备份存储）
CREATE TABLE IF NOT EXISTS klines (
    timestamp   TIMESTAMPTZ NOT NULL,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL
);

-- 转换为时序超表
SELECT create_hypertable('klines', 'timestamp', if_not_exists => TRUE);

-- 成交记录表（比 CSV 更便于查询和分析）
CREATE TABLE IF NOT EXISTS trades (
    id          BIGSERIAL   PRIMARY KEY,
    order_id    TEXT        NOT NULL UNIQUE,
    symbol      TEXT        NOT NULL,
    side        TEXT        NOT NULL,
    filled_qty  DOUBLE PRECISION NOT NULL,
    avg_price   DOUBLE PRECISION NOT NULL,
    fee         DOUBLE PRECISION NOT NULL,
    fee_currency TEXT       NOT NULL DEFAULT 'USDT',
    strategy_id TEXT        NOT NULL,
    exchange    TEXT        NOT NULL,
    mode        TEXT        NOT NULL DEFAULT 'paper',
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 风控事件审计表（持久化审计日志）
CREATE TABLE IF NOT EXISTS audit_events (
    id          BIGSERIAL   PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    payload     JSONB,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_klines_symbol_ts ON klines (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events (event_type, timestamp DESC);

-- 启用 K 线表的时序压缩（降低存储成本）
ALTER TABLE klines SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange,symbol,timeframe'
);
