"""
tests/conftest.py — 全局 pytest Fixtures

包含：
- 每次测试后清理日志状态
- 提供统一的事件总线 fixture（独立实例，不与全局总线共享）
"""

from __future__ import annotations

import pytest

from core.event import EventBus


@pytest.fixture
def event_bus() -> EventBus:
    """返回全新（隔离）的 EventBus 实例，测试间不共享。"""
    return EventBus()
