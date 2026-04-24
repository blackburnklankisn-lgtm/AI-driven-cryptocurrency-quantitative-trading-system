"""modules/data/sentiment/__init__.py — 情绪数据层公共接口"""

from modules.data.sentiment.providers import (
    SENTIMENT_FIELDS,
    SentimentFetchError,
    SentimentProvider,
    SentimentRecord,
    AlternativeMeProvider,
    CryptoCompareProvider,
    MockSentimentProvider,
)
from modules.data.sentiment.cache import SentimentCache
from modules.data.sentiment.collector import SentimentCollectorConfig, SentimentCollector
from modules.data.sentiment.feature_builder import (
    FEATURE_COLUMNS,
    SentimentFeatureBuilderConfig,
    SentimentFeatureBuilder,
)

__all__ = [
    # providers
    "SENTIMENT_FIELDS",
    "SentimentFetchError",
    "SentimentProvider",
    "SentimentRecord",
    "AlternativeMeProvider",
    "CryptoCompareProvider",
    "MockSentimentProvider",
    # cache
    "SentimentCache",
    # collector
    "SentimentCollectorConfig",
    "SentimentCollector",
    # feature builder
    "FEATURE_COLUMNS",
    "SentimentFeatureBuilderConfig",
    "SentimentFeatureBuilder",
]
