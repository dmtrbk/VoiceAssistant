# skills/stocks/__init__.py
# Публичный фасад: from skills.stocks import StocksSkill.

from .common import (
    _BOUGHT_PATH,
    _HOLD_PATH,
    _MOEX_BOARDS,
    _TRADE_PATH,
    _auto_trade_enabled,
    _buy_block_reason,
    _is_api_buy_forbidden_text,
    _min_trade_rub,
    _signal_is_buy,
    _signal_weight,
    _voice_trade_enabled,
    send_telegram_notification,
    telegram_configured,
    trading_clip_limit,
    trading_reason_hint,
    trading_temperature,
)
from .journal import format_journal, format_trade_line, _wants_journal
from .quotes import _wants_market_report
from .skill import StocksSkill
from .trades import (
    _extract_lots,
    _has_max_hint,
    _is_drop_sell,
    _trade_kind,
    _wants_advice,
)

__all__ = [
    "StocksSkill",
    "format_journal",
    "format_trade_line",
    "trading_clip_limit",
    "trading_reason_hint",
    "trading_temperature",
]
