# skills/crypto/journal.py
# Дневник сделок крипты.

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import common
from .common import _JOURNAL_HINTS, _format_usd

logger = logging.getLogger(__name__)


def _wants_journal(text: str) -> bool:
    return any(hint in text for hint in _JOURNAL_HINTS)


def _read_trades() -> list[dict[str, Any]]:
    if not os.path.isfile(common._TRADE_PATH):
        return []
    try:
        with open(common._TRADE_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("trades") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _write_trades(trades: list[dict[str, Any]]) -> None:
    payload = {"trades": trades[-200:]}
    tmp_path = common._TRADE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, common._TRADE_PATH)
    except Exception as exc:
        logger.warning("[Крипта] не записал дневник: %s", exc)


def format_trade_line(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "").strip()
    side = "купил" if str(entry.get("side") or "") == "buy" else "продал"
    name = str(entry.get("name") or entry.get("ticker") or "")
    ticker = str(entry.get("ticker") or "")
    quote = entry.get("quote") or 0
    price = entry.get("price") or 0
    quote_bit = f" на {_format_usd(float(quote))}" if quote else ""
    price_bit = f" по {price:g}" if price else ""
    when = f"{ts} " if ts else ""
    return f"{when}{side} {name} ({ticker}){quote_bit}{price_bit}"


def format_journal(trades: list[dict[str, Any]] | None = None, limit: int = 8) -> str:
    rows = trades if trades is not None else _read_trades()
    if not rows:
        return ""
    lines = [format_trade_line(item) for item in rows[-limit:]]
    return "Дневник крипты:\n" + "\n".join(lines)


class CryptoJournalMixin:
    def _run_journal(self, channel: str = "voice") -> str:
        text = format_journal() or "Дневник пуст: сделок ещё не было."
        if channel == "telegram":
            return text
        if common.telegram_configured():
            common.send_telegram_notification(text, background=False)
            return "Отправил дневник крипты в телеграм."
        return text
    def _journal_trade(self, ticker: str, side: str, quote: float, price: float, spoken: str) -> None:
        entry = {
            "ts": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M"),
            "ticker": ticker,
            "name": spoken,
            "side": "buy" if side == "Buy" else "sell",
            "quote": round(float(quote or 0), 2),
            "price": round(float(price or 0), 6),
        }
        trades = _read_trades()
        trades.append(entry)
        _write_trades(trades)
        try:
            import conky_markets
            conky_markets.poke_refresh()
        except Exception:
            pass
        line = "Дневник крипты: " + format_trade_line(entry)
        logger.info("[Крипта] %s", line)
        if common.telegram_configured():
            common.send_telegram_notification(line, background=True)
