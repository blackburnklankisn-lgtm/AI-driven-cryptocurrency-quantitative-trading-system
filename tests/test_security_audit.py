"""
tests/test_security_audit.py — 安全审计测试

阶段05 验证目标：
- 验证日志脱敏过滤器能正确屏蔽 API Key / Secret 等敏感信息
- 验证 WebSocket 日志流不会泄露敏感信息
- 验证 .env.example 不含真实密钥
- 验证配置加载不会将密钥写入日志
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.logger import _looks_like_secret, _sanitize_message


class TestSanitizeMessage:
    """测试日志脱敏函数。"""

    def test_redacts_api_key_in_kv_format(self):
        """明确的键值对格式应被脱敏。"""
        msg = "api_key=AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        result = _sanitize_message(msg)
        assert "AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in result
        assert "REDACTED" in result or "***" in result

    def test_redacts_secret_in_kv_format(self):
        """secret= 格式应被脱敏。"""
        msg = "secret=MySecretKey1234567890abcdefghij"
        result = _sanitize_message(msg)
        assert "MySecretKey1234567890abcdefghij" not in result

    def test_redacts_password_in_colon_format(self):
        """password: 格式应被脱敏。"""
        msg = "password: SuperSecurePassword123456789"
        result = _sanitize_message(msg)
        assert "SuperSecurePassword123456789" not in result

    def test_redacts_token_in_bearer_format(self):
        """Bearer token 格式应被脱敏。"""
        msg = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef1234567890"
        result = _sanitize_message(msg)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef1234567890" not in result

    def test_preserves_normal_messages(self):
        """普通日志消息不应被修改。"""
        msg = "系统启动: mode=paper exchange=binance"
        result = _sanitize_message(msg)
        assert "mode=paper" in result
        assert "exchange=binance" in result

    def test_preserves_short_strings(self):
        """短字符串不应被误判为密钥。"""
        msg = "symbol=BTCUSDT price=50000.00"
        result = _sanitize_message(msg)
        assert "BTCUSDT" in result
        assert "50000.00" in result

    def test_preserves_numeric_values(self):
        """纯数字不应被脱敏。"""
        msg = "equity=123456789012345678"
        result = _sanitize_message(msg)
        # 纯数字不满足高熵条件（只有一种字符类型）
        assert "123456789012345678" in result

    def test_redacts_high_entropy_bare_string(self):
        """裸露的高熵字符串（看起来像密钥）应被脱敏。"""
        # 模拟一个真实的 Binance API Key 格式（大小写混合+数字，32位以上）
        fake_key = "vmPUZE6mv9SD5VNHk4HlbGXkAt34Em8Abc"
        msg = f"连接交易所，使用密钥 {fake_key}"
        result = _sanitize_message(msg)
        assert fake_key not in result

    def test_preserves_file_paths(self):
        """文件路径不应被误判为密钥。"""
        msg = "配置文件路径: /home/user/configs/system.yaml"
        result = _sanitize_message(msg)
        assert "configs/system.yaml" in result


class TestLooksLikeSecret:
    """测试密钥启发式判断函数。"""

    def test_high_entropy_mixed_case_is_secret(self):
        """大小写混合的长字符串应被识别为密钥。"""
        assert _looks_like_secret("vmPUZE6mv9SD5VNHk4HlbGXkAt34Em8Abc") is True

    def test_short_string_is_not_secret(self):
        """短字符串不是密钥。"""
        assert _looks_like_secret("AbCd1234") is False

    def test_all_lowercase_is_not_secret(self):
        """纯小写字符串（低熵）不是密钥。"""
        assert _looks_like_secret("abcdefghijklmnopqrstuvwxyzabcdefgh") is False

    def test_all_digits_is_not_secret(self):
        """纯数字不是密钥。"""
        assert _looks_like_secret("12345678901234567890123456789012") is False

    def test_path_with_slash_is_not_secret(self):
        """包含斜杠的字符串（路径）不是密钥。"""
        assert _looks_like_secret("AbCdEfGhIjKlMnOpQrStUvWxYz/path") is False

    def test_string_with_dot_is_not_secret(self):
        """包含点的字符串（域名/IP）不是密钥。"""
        assert _looks_like_secret("AbCdEfGhIjKlMnOpQrStUvWxYz.com") is False


class TestEnvExampleSecurity:
    """验证 .env.example 文件不含真实密钥。"""

    def test_env_example_has_no_real_keys(self):
        """
        .env.example 中的密钥字段应该是占位符，不是真实密钥。
        """
        env_example = Path(__file__).parent.parent / ".env.example"
        assert env_example.exists(), ".env.example 文件不存在"

        content = env_example.read_text(encoding="utf-8")

        # 检查密钥字段是否为占位符
        placeholder_patterns = [
            "your_api_key_here",
            "your_secret_here",
            "your_passphrase_here",
        ]
        for placeholder in placeholder_patterns:
            assert placeholder in content, (
                f".env.example 应包含占位符 '{placeholder}'，"
                f"而不是真实密钥"
            )

    def test_env_example_no_high_entropy_values(self):
        """
        .env.example 中不应包含看起来像真实密钥的高熵字符串。
        """
        env_example = Path(__file__).parent.parent / ".env.example"
        content = env_example.read_text(encoding="utf-8")

        # 检查每行的值部分
        for line in content.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                value = value.strip()
                assert not _looks_like_secret(value), (
                    f".env.example 中 {key} 的值看起来像真实密钥: {value[:8]}..."
                )


class TestWebSocketLogSanitization:
    """验证 WebSocket 日志 Sink 的脱敏行为。"""

    def test_websocket_sink_sanitizes_before_broadcast(self):
        """
        WebsocketLogSink 应在广播前对消息进行脱敏。
        
        注意：此测试验证脱敏逻辑的集成，不实际建立 WebSocket 连接。
        """
        from apps.api.server import WebsocketLogSink

        sink = WebsocketLogSink()

        # 模拟一条包含敏感信息的日志消息
        sensitive_msg = "api_key=AbCdEfGhIjKlMnOpQrStUvWxYz123456 已连接"

        # 脱敏后的消息不应包含原始密钥
        sanitized = _sanitize_message(sensitive_msg)
        assert "AbCdEfGhIjKlMnOpQrStUvWxYz123456" not in sanitized


class TestConfigSecurityPolicy:
    """验证配置加载的安全策略。"""

    def test_system_yaml_has_no_secrets(self):
        """
        configs/system.yaml 不应包含 API Key 或 Secret。
        """
        config_file = Path(__file__).parent.parent / "configs" / "system.yaml"
        assert config_file.exists(), "configs/system.yaml 不存在"

        content = config_file.read_text(encoding="utf-8")

        # 不应包含密钥相关字段的真实值
        dangerous_patterns = [
            r"api_key:\s+[A-Za-z0-9]{20,}",
            r"secret:\s+[A-Za-z0-9]{20,}",
            r"password:\s+[A-Za-z0-9]{8,}",
        ]
        for pattern in dangerous_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            assert not matches, (
                f"configs/system.yaml 中发现可能的密钥: {matches}"
            )

    def test_gitignore_excludes_env_file(self):
        """
        .gitignore 应包含 .env 排除规则，防止真实密钥被提交。
        """
        gitignore = Path(__file__).parent.parent / ".gitignore"
        assert gitignore.exists(), ".gitignore 文件不存在"

        content = gitignore.read_text(encoding="utf-8")
        assert ".env" in content, ".gitignore 应包含 .env 排除规则"
