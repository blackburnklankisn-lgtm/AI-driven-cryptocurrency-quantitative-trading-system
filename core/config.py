"""
core/config.py — 配置加载模块

设计原则：
- 使用 pydantic-settings 进行类型验证与环境变量注入
- 支持 YAML 文件 + 环境变量两层覆盖（env > yaml）
- 禁止任何私钥、API Key 在代码或 YAML 中出现
- 配置失败时立即抛出 ConfigError，禁止带错误默认值继续

接口：
    load_config(config_path) → SystemConfig
    get_config()             → 返回已初始化的全局配置单例
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.exceptions import ConfigError


# ══════════════════════════════════════════════════════════════
# 子配置块
# ══════════════════════════════════════════════════════════════

class ExchangeConfig(BaseSettings):
    """交易所连接配置（密钥仅从环境变量读取）。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    exchange_id: str = Field(default="binance", alias="EXCHANGE_ID")

    # ── 密钥：强制来自环境变量，不在 YAML 中配置 ───────────
    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_secret: str = Field(default="", alias="BINANCE_SECRET")
    okx_api_key: str = Field(default="", alias="OKX_API_KEY")
    okx_secret: str = Field(default="", alias="OKX_SECRET")
    okx_passphrase: str = Field(default="", alias="OKX_PASSPHRASE")
    htx_api_key: str = Field(default="", alias="HTX_API_KEY")
    htx_secret: str = Field(default="", alias="HTX_SECRET")

    rate_limit: bool = True          # 是否启用 CCXT 内置限速
    request_timeout_ms: int = 10_000 # 单次请求超时，毫秒

    def get_credentials(self) -> tuple[str, str, str]:
        """
        根据当前 exchange_id 返回对应的 (api_key, secret, passphrase)。
        动态选择，无需在网关初始化时硬编码交易所名称。
        """
        eid = self.exchange_id.lower()
        if eid in ("htx", "huobi", "huobipro"):
            return self.htx_api_key, self.htx_secret, ""
        elif eid == "binance":
            return self.binance_api_key, self.binance_secret, ""
        elif eid == "okx":
            return self.okx_api_key, self.okx_secret, self.okx_passphrase
        else:
            return "", "", ""


class RiskConfig(BaseSettings):
    """风控硬约束参数。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # 单币种最大仓位占总资产的比例
    max_position_pct: Decimal = Field(
        default=Decimal("0.20"),
        alias="MAX_POSITION_PCT",
        description="[0.0, 1.0] 单币种最大仓位占净值比例",
    )
    # 组合最大回撤限制（超过则触发熔断）
    max_portfolio_drawdown: Decimal = Field(
        default=Decimal("0.10"),
        alias="MAX_PORTFOLIO_DRAWDOWN",
        description="[0.0, 1.0] 触发熔断的最大回撤阈值",
    )
    # 单日最大亏损限制
    max_daily_loss: Decimal = Field(
        default=Decimal("0.03"),
        alias="MAX_DAILY_LOSS",
        description="[0.0, 1.0] 单日亏损超过此比例则暂停交易",
    )
    # 连续亏损熔断次数
    max_consecutive_losses: int = 5

    @field_validator("max_position_pct", "max_portfolio_drawdown", "max_daily_loss", mode="before")
    @classmethod
    def must_be_fraction(cls, v: object) -> object:
        val = Decimal(str(v))
        if not (Decimal("0") < val <= Decimal("1")):
            raise ValueError(f"风控比例参数必须在 (0, 1] 范围内，实际值={v}")
        return val


class DataConfig(BaseSettings):
    """数据层配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    default_timeframe: str = "1h"
    supported_timeframes: list[str] = ["1m", "5m", "15m", "1h", "4h", "1d"]
    default_symbols: list[str] = ["BTC/USDT", "ETH/USDT"]
    # 历史数据存储根目录（相对路径）
    storage_dir: str = "./storage"


class PortfolioConfig(BaseSettings):
    """组合管理配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    enabled: bool = True
    allocation_method: str = "risk_parity"  # equal_weight / risk_parity / momentum / minimum_variance
    lookback_bars: int = 60
    weight_cap: float = 0.40
    min_weight: float = 0.0
    rebalance_every_n: int = 24
    drift_threshold: float = 0.05
    min_trade_notional: float = 10.0
    cash_buffer_pct: float = 0.02


class ContinuousLearningConfig(BaseSettings):
    """ML 连续学习配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    enabled: bool = True
    retrain_every_n_bars: int = 500
    min_accuracy_threshold: float = 0.55
    drift_significance: float = 0.05
    drift_check_window: int = 100
    max_buffer_size: int = 10000
    max_saved_versions: int = 3
    ab_test_window: int = 50
    min_bars_for_retrain: int = 400


class Phase3RealtimeFeedConfig(BaseSettings):
    """Phase 3 实时订单簿数据层配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    reconnect_backoff_sec: float = 2.0
    heartbeat_timeout_sec: float = 15.0
    orderbook_depth_levels: int = 20
    snapshot_recovery_enabled: bool = True
    max_gap_tolerance: int = 1


class Phase3MarketMakingConfig(BaseSettings):
    """Phase 3 Avellaneda 做市策略配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    risk_aversion_gamma: float = 0.12
    max_inventory_pct: float = 0.20
    quote_refresh_ms: int = 1500
    cancel_on_gap: bool = True
    max_quote_age_sec: float = 10.0
    maker_only: bool = True


class Phase3RLConfig(BaseSettings):
    """Phase 3 RL 交易代理配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    training_enabled: bool = False
    policy_mode: str = "shadow"          # shadow / paper / active
    reward_drawdown_penalty: float = 2.0
    reward_turnover_penalty: float = 0.2
    action_confidence_floor: float = 0.55
    max_episode_steps: int = 1000


class Phase3EvolutionConfig(BaseSettings):
    """Phase 3 Self-Evolution Engine 配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    weekly_optimization_cron: str = "0 3 * * 0"  # 每周日凌晨 3 点
    shadow_days: int = 7
    paper_days: int = 7
    ab_min_samples: int = 100
    promote_min_sharpe: float = 0.8
    retire_max_drawdown: float = 0.10
    auto_rollback_enabled: bool = True


class Phase3LoggingConfig(BaseSettings):
    """Phase 3 模块可观测性配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    realtime_debug: bool = True
    market_making_debug: bool = True
    rl_debug: bool = True
    evolution_debug: bool = True
    trace_sample_rate: float = 1.0


class Phase3Config(BaseSettings):
    """
    Phase 3 高级策略与自进化功能开关 + 子模块配置。

    所有高级能力默认关闭，必须显式在 system.yaml 或环境变量中开启，
    以确保 Phase 1 / Phase 2 运行时不受影响。
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    enabled: bool = True
    realtime_feed_enabled: bool = False
    market_making_enabled: bool = False
    rl_agent_enabled: bool = False
    self_evolution_enabled: bool = False

    realtime_feed: Phase3RealtimeFeedConfig = Field(
        default_factory=Phase3RealtimeFeedConfig
    )
    market_making: Phase3MarketMakingConfig = Field(
        default_factory=Phase3MarketMakingConfig
    )
    rl: Phase3RLConfig = Field(default_factory=Phase3RLConfig)
    evolution: Phase3EvolutionConfig = Field(default_factory=Phase3EvolutionConfig)
    logging: Phase3LoggingConfig = Field(default_factory=Phase3LoggingConfig)


class LoggingConfig(BaseSettings):
    """日志配置。"""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_dir: str = Field(default="./logs", alias="LOG_DIR")

    @field_validator("log_level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level 必须是 {allowed} 之一，实际={v}")
        return v.upper()


# ══════════════════════════════════════════════════════════════
# 主配置
# ══════════════════════════════════════════════════════════════

class SystemConfig(BaseSettings):
    """
    系统顶层配置，由 YAML 文件 + 环境变量共同组成。
    优先级：环境变量 > YAML > 代码默认值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行模式：live（实盘）/ paper（模拟盘）/ backtest（回测）
    trading_mode: Literal["live", "paper", "backtest"] = Field(
        default="paper",
        alias="TRADING_MODE",
    )

    database_url: str = Field(
        default="sqlite:///./storage/crypto_quant.db",
        alias="DATABASE_URL",
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        alias="REDIS_URL",
    )

    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    continuous_learning: ContinuousLearningConfig = Field(default_factory=ContinuousLearningConfig)
    phase3: Phase3Config = Field(default_factory=Phase3Config)


# ══════════════════════════════════════════════════════════════
# 加载函数与全局单例
# ══════════════════════════════════════════════════════════════

_config: SystemConfig | None = None


def _build_phase3_config(phase3_yaml: dict) -> Phase3Config:
    """
    从 YAML phase3 块构建 Phase3Config，保持各子块的嵌套结构。

    子块（realtime_feed / market_making / rl / evolution / logging）
    各自用对应子配置类的 model_validate 解析；
    顶层开关字段直接透传。
    """
    top_level = {
        k: v for k, v in phase3_yaml.items()
        if k not in ("realtime_feed", "market_making", "rl", "evolution", "logging")
    }
    return Phase3Config.model_validate({
        **top_level,
        "realtime_feed": Phase3RealtimeFeedConfig.model_validate(
            phase3_yaml.get("realtime_feed", {}) or {}
        ),
        "market_making": Phase3MarketMakingConfig.model_validate(
            phase3_yaml.get("market_making", {}) or {}
        ),
        "rl": Phase3RLConfig.model_validate(
            phase3_yaml.get("rl", {}) or {}
        ),
        "evolution": Phase3EvolutionConfig.model_validate(
            phase3_yaml.get("evolution", {}) or {}
        ),
        "logging": Phase3LoggingConfig.model_validate(
            phase3_yaml.get("logging", {}) or {}
        ),
    })


def load_config(yaml_path: str | Path = "configs/system.yaml") -> SystemConfig:
    """
    从 YAML 文件 + 环境变量加载系统配置。

    优先级：os 环境变量 > .env 文件 > YAML 文件 > 代码默认值。

    Args:
        yaml_path: YAML 配置文件路径

    Returns:
        已验证的 SystemConfig 实例

    Raises:
        ConfigError: 配置文件缺失、格式错误或验证失败时
    """
    global _config  # noqa: PLW0603

    # ── 将 .env 文件的值推入 os.environ（不覆盖已存在的环境变量）──────────
    # 这样后续各子配置调用 BaseSettings() 时可从 os.environ 读取 .env 的值。
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(override=False)
    except ImportError:
        pass  # python-dotenv 不可用时降级，不阻断启动

    yaml_file = Path(yaml_path)
    yaml_data: dict = {}

    if yaml_file.exists():
        try:
            with yaml_file.open(encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ConfigError(f"YAML 配置文件解析失败: {yaml_path}") from exc

    try:
        # ── 从 yaml_data 中摘出各子配置段，分别按优先级构建 ────────────────
        # ExchangeConfig / RiskConfig / LoggingConfig：使用 BaseSettings()
        #   构造器，可直接读取 os.environ（含 .env 推入的值），实现 env > default。
        # DataConfig：YAML 提供 default_symbols 等纯配置字段，未被 env 覆盖，
        #   用 model_validate 保留这些值。
        data_yaml = yaml_data.pop("data", {}) or {}
        portfolio_yaml = yaml_data.pop("portfolio", {}) or {}
        cl_yaml = yaml_data.pop("continuous_learning", {}) or {}
        exchange_yaml = yaml_data.pop("exchange", {}) or {}
        phase3_yaml = yaml_data.pop("phase3", {}) or {}
        yaml_data.pop("risk", None)       # 由 RiskConfig()    从 env 读取
        yaml_data.pop("logging", None)    # 由 LoggingConfig() 从 env 读取

        # ── 将 YAML exchange_id 注入 os.environ 作为回退 ──────────────
        # 优先级链：os.environ（Electron 注入）> .env > YAML > 代码默认值
        # 解决安装包在目标 PC 找不到 .env 时回退为 binance 的问题
        _eid = exchange_yaml.get("exchange_id")
        if _eid and "EXCHANGE_ID" not in os.environ:
            os.environ["EXCHANGE_ID"] = str(_eid)

        _config = SystemConfig.model_validate({
            **yaml_data,                                   # 顶层：trading_mode 等
            "exchange": ExchangeConfig(),                  # env 优先：EXCHANGE_ID 等
            "risk": RiskConfig(),                          # env 优先：MAX_* 风控限制
            "data": DataConfig.model_validate(data_yaml),  # YAML 优先：default_symbols 等
            "logging": LoggingConfig(),                    # env 优先：LOG_LEVEL 等
            "portfolio": PortfolioConfig.model_validate(portfolio_yaml),
            "continuous_learning": ContinuousLearningConfig.model_validate(cl_yaml),
            "phase3": _build_phase3_config(phase3_yaml),
        })
    except Exception as exc:
        raise ConfigError(f"系统配置验证失败: {exc}") from exc

    return _config


def get_config() -> SystemConfig:
    """
    返回全局配置单例。

    必须先调用 load_config() 完成初始化。

    Raises:
        ConfigError: 未初始化时访问
    """
    if _config is None:
        raise ConfigError("配置未初始化，请先调用 load_config()")
    return _config
