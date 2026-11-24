"""Bybit v5 USDT Perpetual (linear) contour: strategy core + paper/live desk.

Базовый спот-слой (WS/IOC/reconcile/Earn/журналы) не ломаем.
Автоспот-алго сняты; linear — отдельный контур.
"""

from .config import LinearConfig, default_config
from .settings_store import LinearSettings, current as linear_settings, status_line as linear_status_line
from .strategy_core import Signal, StrategyParams, build_features, signal_at_bar, size_leg_usd

__all__ = [
    "LinearConfig",
    "LinearSettings",
    "default_config",
    "linear_settings",
    "linear_status_line",
    "Signal",
    "StrategyParams",
    "build_features",
    "signal_at_bar",
    "size_leg_usd",
]
