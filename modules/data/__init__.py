"""modules/data/__init__.py"""
from modules.data.downloader import KlineDownloader
from modules.data.feed import DataFeed
from modules.data.storage import ParquetStorage
from modules.data.validator import KlineValidator

# Phase 2 W12 新增
from modules.data.fusion import (
    FreshnessStatus,
    SourceFreshness,
    SourceFrame,
    FreshnessConfig,
    FreshnessEvaluator,
    SourceAligner,
)
from modules.data.onchain import (
    OnChainProvider,
    OnChainRecord,
    MockOnChainProvider,
    OnChainCache,
    OnChainCollector,
    OnChainFeatureBuilder,
)

# Phase 2 W13 新增
from modules.data.sentiment import (
    SentimentProvider,
    SentimentRecord,
    MockSentimentProvider,
    SentimentCache,
    SentimentCollector,
    SentimentFeatureBuilder,
)

__all__ = [
    # Phase 1
    "KlineDownloader",
    "KlineValidator",
    "ParquetStorage",
    "DataFeed",
    # Phase 2 W12 — fusion
    "FreshnessStatus",
    "SourceFreshness",
    "SourceFrame",
    "FreshnessConfig",
    "FreshnessEvaluator",
    "SourceAligner",
    # Phase 2 W12 — onchain
    "OnChainProvider",
    "OnChainRecord",
    "MockOnChainProvider",
    "OnChainCache",
    "OnChainCollector",
    "OnChainFeatureBuilder",
    # Phase 2 W13 — sentiment
    "SentimentProvider",
    "SentimentRecord",
    "MockSentimentProvider",
    "SentimentCache",
    "SentimentCollector",
    "SentimentFeatureBuilder",
]
