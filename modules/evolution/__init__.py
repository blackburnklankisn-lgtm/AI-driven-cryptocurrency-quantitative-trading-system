"""
modules/evolution/__init__.py — 自进化模块公开导出
"""

from modules.evolution.ab_test_manager import ABExperimentConfig, ABTestManager, ABResult
from modules.evolution.candidate_registry import CandidateRegistry
from modules.evolution.promotion_gate import PromotionGate, PromotionGateConfig, StageGateConfig
from modules.evolution.report_builder import ReportBuilder
from modules.evolution.retirement_policy import RetirementConfig, RetirementPolicy
from modules.evolution.scheduler import EvolutionScheduler, SchedulerConfig
from modules.evolution.self_evolution_engine import SelfEvolutionConfig, SelfEvolutionEngine
from modules.evolution.state_store import EvolutionStateStore

__all__ = [
    "ABExperimentConfig",
    "ABTestManager",
    "ABResult",
    "CandidateRegistry",
    "EvolutionScheduler",
    "EvolutionStateStore",
    "PromotionGate",
    "PromotionGateConfig",
    "StageGateConfig",
    "ReportBuilder",
    "RetirementConfig",
    "RetirementPolicy",
    "SchedulerConfig",
    "SelfEvolutionConfig",
    "SelfEvolutionEngine",
]
