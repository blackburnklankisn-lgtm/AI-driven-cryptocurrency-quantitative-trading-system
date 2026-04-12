"""
core/logger.py — 统一日志工厂

设计原则：
- 使用 loguru 为每个模块提供结构化日志
- 日志必须包含：时间戳、模块名称、级别、消息、可选结构化字段
- 所有关键事件（下单、风控拦截、熔断、异常）写入独立的审计日志文件
- 审计日志不可在代码中屏蔽；运行日志可按级别过滤
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# ── 内部状态：防止重复初始化 ───────────────────────────────
_initialized = False


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

    # ── 控制台输出（彩色） ─────────────────────────────────
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
    )

    # ── 运行日志文件（按日轮转） ───────────────────────────
    logger.add(
        log_path / "system_{time:YYYY-MM-DD}.log",
        level=log_level,
        rotation="00:00",
        retention=retention,
        compression="zip",
        enqueue=True,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    )

    # ── 审计日志文件（CRITICAL 级，不可屏蔽） ──────────────
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
    )

    _initialized = True
    logger.info("日志系统初始化完成，日志目录={}", log_path.resolve())


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
