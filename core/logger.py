"""
core/logger.py — 统一日志工厂

设计原则：
- 使用 loguru 为每个模块提供结构化日志
- 日志必须包含：时间戳、模块名称、级别、消息、可选结构化字段
- 所有关键事件（下单、风控拦截、熔断、异常）写入独立的审计日志文件
- 审计日志不可在代码中屏蔽；运行日志可按级别过滤
- 安全：所有日志输出经过脱敏过滤，防止 API Key 等敏感信息泄露
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from loguru import logger

# ── 内部状态：防止重复初始化 ───────────────────────────────
_initialized = False

# ── 敏感信息脱敏正则表达式 ────────────────────────────────
# 匹配常见的 API Key / Secret 模式（16-64位字母数字字符串）
_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 明确的键值对格式: key=VALUE 或 key: VALUE
    (re.compile(r'(?i)(api[_-]?key|secret|passphrase|password|token|bearer)\s*[=:]\s*([A-Za-z0-9+/=_\-]{16,})', re.IGNORECASE),
     r'\1=***REDACTED***'),
    # 环境变量注入的密钥（通常是长字母数字串，超过32位）
    (re.compile(r'\b([A-Za-z0-9]{32,})\b'),
     lambda m: '***' + m.group(1)[:4] + '...' + m.group(1)[-4:] + '***'
     if _looks_like_secret(m.group(1)) else m.group(0)),
]


def _looks_like_secret(s: str) -> bool:
    """
    启发式判断一个字符串是否看起来像密钥。
    
    判断标准：
    - 长度 >= 32
    - 包含大小写混合或数字混合（熵值较高）
    - 不是常见的单词或路径
    """
    if len(s) < 32:
        return False
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    has_digit = any(c.isdigit() for c in s)
    # 需要至少两种字符类型混合（高熵特征）
    char_types = sum([has_upper, has_lower, has_digit])
    if char_types < 2:
        return False
    # 排除常见的非密钥长字符串（如文件路径、URL）
    if '/' in s or '\\' in s or '.' in s or '-' in s:
        return False
    return True


def _sanitize_message(message: str) -> str:
    """
    对日志消息进行脱敏处理。
    
    优先匹配明确的键值对格式，再做高熵字符串检测。
    """
    # 第一步：匹配明确的键值对格式
    pattern, replacement = _SENSITIVE_PATTERNS[0]
    message = pattern.sub(replacement, message)
    
    # 第二步：检测高熵字符串（可能是裸露的密钥）
    # 注意：此步骤较保守，只过滤明确看起来像密钥的字符串
    pattern2, replacement2 = _SENSITIVE_PATTERNS[1]
    message = pattern2.sub(replacement2, message)
    
    return message


class _SanitizingFilter:
    """
    Loguru 脱敏过滤器。
    
    在日志消息到达任何 sink（控制台、文件、WebSocket）之前，
    对消息内容进行敏感信息过滤。
    """
    def __call__(self, record: dict) -> bool:
        # 对消息进行脱敏（原地修改 record）
        record["message"] = _sanitize_message(str(record["message"]))
        # 对 extra 字段也进行脱敏
        if record.get("extra"):
            for key, value in record["extra"].items():
                if isinstance(value, str):
                    record["extra"][key] = _sanitize_message(value)
        return True  # 始终允许日志通过（只做脱敏，不过滤）


# 全局脱敏过滤器实例
_sanitizing_filter = _SanitizingFilter()


def setup_logging(
    log_dir: str | Path = "./logs",
    log_level: str = "INFO",
    rotation: str = "50 MB",
    retention: str = "30 days",
) -> None:
    """
    初始化全局日志配置，应在程序入口处调用一次。

    Args:
        log_dir:    日志文件目录
        log_level:  控制台日志级别 (DEBUG/INFO/WARNING/ERROR)
        rotation:   单文件最大体积后触发轮转
        retention:  日志文件保留周期
    """
    global _initialized  # noqa: PLW0603
    if _initialized:
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 移除 loguru 默认处理器
    logger.remove()

    # ── 控制台输出（彩色，带脱敏过滤） ────────────────────
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "{message}"
        ),
        colorize=True,
        enqueue=True,
        filter=_sanitizing_filter,
    )

    # ── 运行日志文件（按日轮转，带脱敏过滤） ──────────────
    logger.add(
        log_path / "system_{time:YYYY-MM-DD}.log",
        level=log_level,
        rotation="00:00",
        retention=retention,
        compression="zip",
        enqueue=True,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        filter=_sanitizing_filter,
    )

    # ── 审计日志文件（CRITICAL 级，不可屏蔽，带脱敏过滤） ──
    # 用于记录所有下单操作、风控拦截、熔断事件
    logger.add(
        log_path / "audit_{time:YYYY-MM-DD}.log",
        level="CRITICAL",
        rotation=rotation,
        retention="90 days",  # 审计日志保留更长时间
        compression="zip",
        enqueue=True,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | AUDIT | {name}:{function}:{line} | {message}",
        filter=_sanitizing_filter,
    )

    _initialized = True
    logger.info("日志系统初始化完成，日志目录={}，脱敏过滤器已启用", log_path.resolve())


def get_logger(name: str) -> "logger":  # type: ignore[valid-type]
    """
    为指定模块返回绑定了 name 上下文的 logger 实例。

    Usage:
        from core.logger import get_logger
        log = get_logger(__name__)
        log.info("消息")
    """
    return logger.bind(module=name)


def audit_log(event_type: str, **kwargs: object) -> None:
    """
    写入强制审计日志（CRITICAL 级别）。

    所有影响资金状态的事件必须通过此函数记录，包括：
    - 下单/撤单/成交确认
    - 风控拦截
    - 熔断触发与恢复

    Args:
        event_type: 事件类型字符串，如 'ORDER_SUBMITTED'
        **kwargs:   事件相关结构化字段
    """
    fields = " | ".join(f"{k}={v!r}" for k, v in kwargs.items())
    logger.critical("[AUDIT] event={} | {}", event_type, fields)
