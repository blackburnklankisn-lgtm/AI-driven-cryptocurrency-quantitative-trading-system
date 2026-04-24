"""
modules/data/realtime/__init__.py — 实时数据层公共接口

Phase 3 W15-W16 实现：订单簿 + 成交流 + 微观结构特征

公开接口：
    orderbook_types   → OrderBookSnapshot, TradeTick, OrderBookDelta, DepthLevel, GapStatus, TradeSide
    ws_client         → ExchangeWsClient, MockWsClient, HtxMarketWsClient, create_exchange_ws_client, WsClientConfig, MockWsClientConfig, WsConnectionState
    depth_cache       → DepthCache, DepthCacheRegistry, DepthCacheConfig
    trade_cache       → TradeCache, TradeCacheRegistry, TradeCacheConfig, TradeFlowStats
    feature_builder   → MicroFeatureBuilder, MicroFeatureFrame, MicroFeatureBuilderConfig, MICRO_FEATURE_COLS
    replay_feed       → ReplayFeed, ReplayFeedConfig, ReplayState
    subscription_manager → SubscriptionManager, SubscriptionManagerConfig, FeedHealth, FeedHealthSnapshot
"""

from modules.data.realtime.orderbook_types import (
    DepthLevel,
    GapStatus,
    OrderBookDelta,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)
from modules.data.realtime.ws_client import (
    ExchangeWsClient,
    HtxMarketWsClient,
    MockWsClient,
    MockWsClientConfig,
    WsClientConfig,
    WsConnectionState,
    create_exchange_ws_client,
)
from modules.data.realtime.depth_cache import (
    DepthCache,
    DepthCacheConfig,
    DepthCacheRegistry,
)
from modules.data.realtime.trade_cache import (
    TradeCache,
    TradeCacheConfig,
    TradeCacheRegistry,
    TradeFlowStats,
)
from modules.data.realtime.feature_builder import (
    MICRO_FEATURE_COLS,
    MicroFeatureBuilder,
    MicroFeatureBuilderConfig,
    MicroFeatureFrame,
)
from modules.data.realtime.replay_feed import (
    ReplayFeed,
    ReplayFeedConfig,
    ReplayState,
)
from modules.data.realtime.subscription_manager import (
    FeedHealth,
    FeedHealthSnapshot,
    SubscriptionManager,
    SubscriptionManagerConfig,
)

__all__ = [
    # orderbook_types
    "DepthLevel",
    "GapStatus",
    "OrderBookDelta",
    "OrderBookSnapshot",
    "TradeSide",
    "TradeTick",
    # ws_client
    "ExchangeWsClient",
    "HtxMarketWsClient",
    "MockWsClient",
    "MockWsClientConfig",
    "WsClientConfig",
    "WsConnectionState",
    "create_exchange_ws_client",
    # depth_cache
    "DepthCache",
    "DepthCacheConfig",
    "DepthCacheRegistry",
    # trade_cache
    "TradeCache",
    "TradeCacheConfig",
    "TradeCacheRegistry",
    "TradeFlowStats",
    # feature_builder
    "MICRO_FEATURE_COLS",
    "MicroFeatureBuilder",
    "MicroFeatureBuilderConfig",
    "MicroFeatureFrame",
    # replay_feed
    "ReplayFeed",
    "ReplayFeedConfig",
    "ReplayState",
    # subscription_manager
    "FeedHealth",
    "FeedHealthSnapshot",
    "SubscriptionManager",
    "SubscriptionManagerConfig",
]
