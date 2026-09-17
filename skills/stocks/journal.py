# skills/stocks/journal.py
# Дневник сделок: файл, голос, Telegram.

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import common
from .common import _JOURNAL_HINTS, _lots_phrase

logger = logging.getLogger(__name__)


def _read_trades(path: str | None = None) -> list[dict[str, Any]]:
    path = path or common._TRADE_PATH
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("trades") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _write_trades(trades: list[dict[str, Any]], path: str | None = None) -> None:
    path = path or common._TRADE_PATH
    payload = {"trades": trades[-200:]}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Биржа] не записал дневник: %s", exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def format_trade_line(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "").strip()
    side = "купил" if str(entry.get("side") or "") == "buy" else "продал"
    name = str(entry.get("name") or entry.get("ticker") or "")
    ticker = str(entry.get("ticker") or "")
    lots = entry.get("lots") or 0
    price = entry.get("price") or 0
    price_bit = f" по {price:g} ₽" if price else ""
    when = f"{ts} " if ts else ""
    return f"{when}{side} {_lots_phrase(int(lots))}: {name} ({ticker}){price_bit}"


def format_journal(trades: list[dict[str, Any]] | None = None, limit: int = 8) -> str:
    rows = trades if trades is not None else _read_trades()
    if not rows:
        return ""
    lines = [format_trade_line(item) for item in rows[-limit:]]
    return "Дневник сделок:\n" + "\n".join(lines)
def _wants_journal(text: str) -> bool:
    return any(hint in text for hint in _JOURNAL_HINTS)


class StocksJournalMixin:
    def _run_journal(self, channel: str = "voice") -> str:
        text = format_journal() or "Дневник пуст: сделок ещё не было."
        if channel == "telegram":
            return text
        if common.telegram_configured():
            common.send_telegram_notification(text, background=False)
            return "Отправил дневник в телеграм."
        return text
    def _journal_trade(self, ticker: str, direction: str, lots: int, spoken: str) -> None:
        ticker = ticker.upper()
        price = 0.0
        try:
            _name, price, _pct = self._quote(ticker)
        except Exception:
            price = 0.0
        entry = {
            "ts": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M"),
            "ticker": ticker,
            "name": spoken,
            "side": "buy" if direction == "ORDER_DIRECTION_BUY" else "sell",
            "lots": int(lots),
            "price": round(float(price or 0), 4),
        }
        trades = _read_trades()
        trades.append(entry)
        _write_trades(trades)
        try:
            import conky_markets
            conky_markets.poke_refresh()
        except Exception:
            pass
        line = "Дневник: " + format_trade_line(entry)
        logger.info("[Биржа] %s", line)
        if common.telegram_configured():
            common.send_telegram_notification(line, background=True)
