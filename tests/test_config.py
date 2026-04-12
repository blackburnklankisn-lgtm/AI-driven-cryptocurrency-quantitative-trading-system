"""
tests/test_config.py — 配置加载单元测试

覆盖项：
- YAML 文件正常加载
- 环境变量覆盖 YAML 值
- 风控参数范围验证（越界应失败）
- 未初始化时访问 get_config() 应失败
- ConfigError 提前抛出（不带错误默认值继续）
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from core.config import RiskConfig, SystemConfig, get_config, load_config
from core.exceptions import ConfigError


@pytest.fixture(autouse=True)
def reset_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试前重置全局配置单例。"""
    import core.config as cfg_module
    monkeypatch.setattr(cfg_module, "_config", None)
    yield
    monkeypatch.setattr(cfg_module, "_config", None)


@pytest.fixture
def minimal_yaml(tmp_path: Path) -> Path:
    """生成一个最小有效的 YAML 配置文件。"""
    cfg = {
        "trading_mode": "paper",
        "risk": {
            "max_position_pct": 0.20,
            "max_portfolio_drawdown": 0.10,
            "max_daily_loss": 0.03,
            "max_consecutive_losses": 5,
        },
    }
    path = tmp_path / "system.yaml"
    path.write_text(yaml.dump(cfg), encoding="utf-8")
    return path


class TestLoadConfig:
    def test_loads_from_yaml(self, minimal_yaml: Path) -> None:
        """正常 YAML 文件应成功加载并返回 SystemConfig。"""
        config = load_config(minimal_yaml)
        assert isinstance(config, SystemConfig)
        assert config.trading_mode == "paper"

    def test_missing_yaml_uses_defaults(self, tmp_path: Path) -> None:
        """YAML 文件不存在时，应使用代码默认值而非报错。"""
        non_existent = tmp_path / "nonexistent.yaml"
        config = load_config(non_existent)
        assert config.trading_mode == "paper"  # 代码默认值

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        """格式错误的 YAML 文件应抛出 ConfigError。"""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{\x00invalid: yaml: content: [", encoding="utf-8")
        with pytest.raises(ConfigError, match="YAML 配置文件解析失败"):
            load_config(bad_file)

    def test_env_var_overrides_yaml(
        self, minimal_yaml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量应覆盖 YAML 中的对应值。"""
        monkeypatch.setenv("TRADING_MODE", "live")
        config = load_config(minimal_yaml)
        assert config.trading_mode == "live"

    def test_get_config_before_load_raises(self) -> None:
        """未调用 load_config() 时调用 get_config() 应抛出 ConfigError。"""
        with pytest.raises(ConfigError, match="配置未初始化"):
            get_config()

    def test_get_config_after_load(self, minimal_yaml: Path) -> None:
        """load_config() 之后调用 get_config() 返回相同实例。"""
        cfg1 = load_config(minimal_yaml)
        cfg2 = get_config()
        assert cfg1 is cfg2


class TestRiskConfig:
    def test_valid_risk_params(self) -> None:
        """合法的风控参数应成功验证。"""
        # 使用 model_validate 绕过 pydantic-settings 的 env var 读取
        risk = RiskConfig.model_validate({
            "MAX_POSITION_PCT": "0.15",
            "MAX_PORTFOLIO_DRAWDOWN": "0.08",
            "MAX_DAILY_LOSS": "0.02",
        })
        assert risk.max_position_pct == Decimal("0.15")

    def test_zero_position_raises(self) -> None:
        """仓位比例不能为 0，应抛出验证错误。"""
        with pytest.raises(Exception):
            RiskConfig.model_validate({
                "MAX_POSITION_PCT": "0",
            })

    def test_greater_than_one_raises(self) -> None:
        """仓位比例不能超过 1，应抛出验证错误。"""
        with pytest.raises(Exception):
            RiskConfig.model_validate({
                "MAX_POSITION_PCT": "1.5",
            })

    def test_negative_value_raises(self) -> None:
        """负值应抛出验证错误。"""
        with pytest.raises(Exception):
            RiskConfig.model_validate({
                "MAX_DAILY_LOSS": "-0.05",
            })
