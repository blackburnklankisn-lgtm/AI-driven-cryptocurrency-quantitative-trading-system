"""modules/risk/__init__.py"""
from modules.risk.manager import RiskManager, RiskConfig
from modules.risk.position_sizer import PositionSizer

# Phase 2 W9-W10 新增
from modules.risk.snapshot import RiskSnapshot, RiskPlan
from modules.risk.cooldown import CooldownManager
from modules.risk.exit_planner import ExitPlanner, ExitPlanConfig, ExitPlan
from modules.risk.dca_engine import DCAEngine, DCAConfig
from modules.risk.adaptive_matrix import AdaptiveRiskMatrix, AdaptiveRiskMatrixConfig

# Phase 2 W11 新增
from modules.risk.state_store import StateStore
from modules.risk.budget_checker import BudgetChecker, BudgetConfig
from modules.risk.kill_switch import KillSwitch, KillSwitchConfig

__all__ = [
    # Phase 1
    "RiskManager",
    "RiskConfig",
    "PositionSizer",
    # Phase 2 W9-W10
    "RiskSnapshot",
    "RiskPlan",
    "CooldownManager",
    "ExitPlanner",
    "ExitPlanConfig",
    "ExitPlan",
    "DCAEngine",
    "DCAConfig",
    "AdaptiveRiskMatrix",
    "AdaptiveRiskMatrixConfig",
    # Phase 2 W11
    "StateStore",
    "BudgetChecker",
    "BudgetConfig",
    "KillSwitch",
    "KillSwitchConfig",
]
