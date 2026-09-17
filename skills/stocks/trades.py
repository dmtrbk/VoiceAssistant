# skills/stocks/trades.py
# Голосовые заявки: купи / продай / переложи.

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from . import common
from .common import (
    _ADVICE_HINTS,
    _ALLIN_HINTS,
    _AUTO_HINTS,
    _BUY_HINTS,
    _LOT_WORDS,
    _MARKET_WORDS,
    _MAX_HINTS,
    _MAX_LOTS,
    _ORDERS,
    _SELL_HINTS,
    _is_api_buy_forbidden_text,
    _lots_phrase,
    _quotation_to_float,
)

logger = logging.getLogger(__name__)


def _wants_advice(text: str) -> bool:
    """Разбор сигналов для хозяина, без заявок."""
    if any(hint in text for hint in _ADVICE_HINTS):
        return True
    return "совет" in text and any(word in text for word in _MARKET_WORDS)


def _wants_market_report(text: str) -> bool:
    """Сводка по счёту, а не «что такое акции» и не болтовня про торговлю."""
    if any(phrase in text for phrase in _ENCYCLOPEDIA):
        return False
    if not any(word in text for word in _MARKET_WORDS):
        return False
    if any(ask in text for ask in _STATUS_ASK):
        return True
    return len(text.split()) <= 3


def _has_max_hint(text: str) -> bool:
    return any(
        re.search(rf"(?<![а-яёa-z]){re.escape(hint)}(?![а-яёa-z])", text)
        for hint in _MAX_HINTS
    )


def _is_drop_sell(text: str) -> bool:
    """«Сбрось» = продажа только вместе с бумагой / рынком, не «сбрось таймер»."""
    if not re.search(r"(?<![а-яёa-z])сбрось(?![а-яёa-z])", text):
        return False
    return any(
        word in text
        for word in (*_MARKET_WORDS, "сбер", "газпром", "втб", "лот", "бумаг", "позици", "тмос")
    )


def _trade_kind(text: str) -> str | None:
    if any(hint in text for hint in _AUTO_HINTS):
        return "auto"
    if any(hint in text for hint in _ALLIN_HINTS):
        return "allin"
    if any(hint in text for hint in _SELL_HINTS) or _is_drop_sell(text):
        return "sell"
    if any(hint in text for hint in _BUY_HINTS):
        return "buy"
    if "вложи" in text:
        return "allin" if _has_max_hint(text) else "buy"
    return None


def _extract_lots(text: str) -> int | None:
    """None — явный «всё». Без числа — один лот, не весь кэш."""
    if _has_max_hint(text):
        return None
    match = re.search(r"\b(\d+)\b", text)
    if match:
        value = int(match.group(1))
        return value if value > 0 else 1
    for word, value in sorted(_LOT_WORDS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![а-яa-z]){word}(?![а-яa-z])", text):
            return value
    return 1



class StocksTradesMixin:
    def _instrument_ids(self, ticker: str) -> tuple[str | None, str | None, str]:
        inst = self._instrument(None, ticker)
        uid = str(inst.get("uid") or inst.get("instrumentId") or inst.get("positionUid") or "") or None
        figi = str(inst.get("figi") or "") or None
        spoken = self._spoken_name(ticker, inst)
        if not uid and not figi:
            raise RuntimeError(f"unknown ticker {ticker}")
        self._remember_name(ticker, spoken)
        return uid, figi, spoken

    def _max_lots(self, ticker: str) -> tuple[int, int]:
        account_id = self._pick_account_id()
        if not account_id:
            raise RuntimeError("no account")
        uid, figi, _spoken = self._instrument_ids(ticker)
        body: dict[str, Any] = {"accountId": account_id}
        if uid:
            body["instrumentId"] = uid
        if figi:
            body["figi"] = figi
        data = self._post(self._url(_MAX_LOTS), body)

        buy_limits = data.get("buyLimits") if isinstance(data.get("buyLimits"), dict) else {}
        sell_limits = data.get("sellLimits") if isinstance(data.get("sellLimits"), dict) else {}

        buy_raw = (
            buy_limits.get("buyMaxMarketLots")
            or buy_limits.get("buyMaxLots")
            or data.get("buyMaxMarketLots")
            or data.get("buyMaxLots")
            or 0
        )
        sell_raw = (
            sell_limits.get("sellMaxLots")
            or data.get("sellMaxLots")
            or 0
        )
        try:
            buy = int(buy_raw)
        except (TypeError, ValueError):
            buy = int(_quotation_to_float(buy_raw) if isinstance(buy_raw, dict) else 0)
        try:
            sell = int(sell_raw)
        except (TypeError, ValueError):
            sell = int(_quotation_to_float(sell_raw) if isinstance(sell_raw, dict) else 0)
        return max(buy, 0), max(sell, 0)

    def _order_filled(self, data: dict[str, Any]) -> bool:
        status = str(data.get("executionReportStatus") or data.get("status") or "").upper()
        if any(marker in status for marker in ("REJECT", "CANCEL")):
            return False
        if not status:
            return False
        return any(marker in status for marker in ("FILL", "PARTIAL"))

    def _place_order(self, ticker: str, direction: str, lots: int) -> str:
        if lots <= 0:
            raise RuntimeError("no lots")
        ticker = ticker.upper()
        if direction == "ORDER_DIRECTION_BUY" and self._is_buy_blocked(ticker):
            raise RuntimeError("api forbidden")
        account_id = self._pick_account_id()
        if not account_id:
            raise RuntimeError("no account")
        uid, figi, spoken = self._instrument_ids(ticker)
        body: dict[str, Any] = {
            "quantity": int(lots),
            "direction": direction,
            "accountId": account_id,
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": str(uuid.uuid4()),
        }
        if uid:
            body["instrumentId"] = uid
        if figi:
            body["figi"] = figi
        try:
            data = self._post(self._url(_ORDERS), body)
        except RuntimeError as exc:
            if direction == "ORDER_DIRECTION_BUY" and (
                "api forbidden" in str(exc) or _is_api_buy_forbidden_text(str(exc))
            ):
                self._buy_blocked.add(ticker)
                self._recommend_manual_buys([ticker])
                if "api forbidden" not in str(exc):
                    raise RuntimeError("api forbidden") from exc
            raise
        self._bust_broker_cache()
        if not self._order_filled(data):
            message = str(data.get("message") or data.get("rejectReason") or "заявка не прошла")
            raise RuntimeError(message)
        if direction == "ORDER_DIRECTION_BUY":
            self._mark_desk_bought(ticker)
        verb = "купил" if direction == "ORDER_DIRECTION_BUY" else "продал"
        phrase = f"{verb.capitalize()} {_lots_phrase(lots)}: {spoken}."
        self._journal_trade(ticker, direction, lots, spoken)
        return phrase

    def _resolve_trade_ticker(self, ticker: str | None) -> str | None:
        if ticker:
            return ticker
        watch = self._watch_tickers()
        if len(watch) == 1:
            return watch[0]
        if self._last_tickers:
            return self._last_tickers[-1]
        return None

    def _execute_trade(self, text: str, kind: str, ticker: str | None) -> str:
        with common._TRADE_LOCK:
            return self._execute_trade_locked(text, kind, ticker)

    def _execute_trade_locked(self, text: str, kind: str, ticker: str | None) -> str:
        if not common._voice_trade_enabled():
            return "Сделки выключены."
        requested = _extract_lots(text)
        if kind == "auto":
            return self._trade_auto()
        if not self._token:
            return "Без торгового ключа заявки не выставляю."
        if not self._market_open():
            raise RuntimeError("market closed")
        if kind == "allin":
            target = self._resolve_trade_ticker(ticker)
            if not target:
                return "Куда перекладывать? Назови бумагу."
            return self._trade_all_in(target)
        target = self._resolve_trade_ticker(ticker)
        if not target:
            return "Какую бумагу? Назови втб или фонд."
        if kind == "buy":
            return self._trade_buy(target, requested)
        return self._trade_sell(target, requested)

    def _trade_buy(self, ticker: str, requested: int | None) -> str:
        ticker = ticker.upper()
        if self._is_buy_blocked(ticker):
            raise RuntimeError("api forbidden")
        buy_max, _sell_max = self._max_lots(ticker)
        if buy_max <= 0:
            raise RuntimeError("no lots")
        lots = buy_max if requested is None else min(requested, buy_max)
        if lots <= 0:
            raise RuntimeError("no lots")
        return self._place_order(ticker, "ORDER_DIRECTION_BUY", lots)

    def _trade_sell(self, ticker: str, requested: int | None) -> str:
        _buy_max, sell_max = self._max_lots(ticker)
        if sell_max <= 0:
            raise RuntimeError("no lots")
        lots = sell_max if requested is None else min(requested, sell_max)
        if lots <= 0:
            raise RuntimeError("no lots")
        return self._place_order(ticker, "ORDER_DIRECTION_SELL", lots)

    def _trade_all_in(self, ticker: str) -> str:
        ticker = ticker.upper()
        if self._is_buy_blocked(ticker):
            raise RuntimeError("api forbidden")
        parts: list[str] = []
        positions, _day, _total = self._safe_positions()
        for item in positions:
            other = item["ticker"]
            if other == ticker:
                continue
            _buy, sell_max = self._max_lots(other)
            if sell_max > 0:
                parts.append(self._place_order(other, "ORDER_DIRECTION_SELL", sell_max))
                time.sleep(0.6)
        time.sleep(0.4)
        self._bust_broker_cache()
        buy_max, _sell = self._max_lots(ticker)
        if buy_max <= 0:
            if parts:
                return " ".join(parts) + " На покупку свободных денег не осталось."
            raise RuntimeError("no lots")
        parts.append(self._place_order(ticker, "ORDER_DIRECTION_BUY", buy_max))
        return " ".join(parts)
