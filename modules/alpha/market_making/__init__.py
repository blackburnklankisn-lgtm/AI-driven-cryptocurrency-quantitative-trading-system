"""
modules/alpha/market_making — Avellaneda-Stoikov 做市策略模块

公开导出所有子模块的核心类，供外部（strategy orchestrator / tests）使用。
"""

from modules.alpha.market_making.avellaneda_model import AvellanedaConfig, AvellanedaModel
from modules.alpha.market_making.fill_simulator import FillSimulator, FillSimulatorConfig
from modules.alpha.market_making.inventory_manager import InventoryConfig, InventoryManager
from modules.alpha.market_making.quote_engine import QuoteEngine, QuoteEngineConfig
from modules.alpha.market_making.quote_lifecycle import QuoteLifecycle, QuoteLifecycleConfig
from modules.alpha.market_making.quote_state_store import QuoteStateSnapshot, QuoteStateStore
from modules.alpha.market_making.strategy import MarketMakingStrategy, MarketMakingStrategyConfig

__all__ = [
    "AvellanedaConfig",
    "AvellanedaModel",
    "FillSimulator",
    "FillSimulatorConfig",
    "InventoryConfig",
    "InventoryManager",
    "QuoteEngine",
    "QuoteEngineConfig",
    "QuoteLifecycle",
    "QuoteLifecycleConfig",
    "QuoteStateSnapshot",
    "QuoteStateStore",
    "MarketMakingStrategy",
    "MarketMakingStrategyConfig",
]
