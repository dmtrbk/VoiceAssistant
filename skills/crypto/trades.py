# skills/crypto/trades.py
# Голосовые заявки спота Bybit.

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import common
from .common import (
    _ALLIN_HINTS,
    _AMT_WORDS,
    _AUTO_HINTS,
    _BUY_HINTS,
    _MAX_HINTS,
    _MIN_QUOTE,
    _SELL_HINTS,
    _format_usd,
    _qty_str,
    _spoken,
)

logger = logging.getLogger(__name__)


def _has_max_hint(text: str) -> bool:
    return any(re.search(rf"(?<![а-яa-z]){re.escape(word)}(?![а-яa-z])", text) for word in _MAX_HINTS)


def _trade_kind(text: str) -> str | None:
    if any(hint in text for hint in _AUTO_HINTS):
        return "auto"
    if any(hint in text for hint in _ALLIN_HINTS):
        return "allin"
    if any(hint in text for hint in _SELL_HINTS):
        return "sell"
    if any(hint in text for hint in _BUY_HINTS):
        return "buy"
    if "вложи" in text:
        return "allin" if _has_max_hint(text) else "buy"
    return None


def _extract_quote(text: str) -> float | None:
    """None — всё. 0 — минимум биржи. Иначе сумма в USDT («на 20 долларов»)."""
    if _has_max_hint(text):
        return None
    match = re.search(r"(?:на|за)\s+(\d+(?:[.,]\d+)?)", text)
    if match:
        return float(match.group(1).replace(",", "."))
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:доллар|тетер|usdt)", text)
    if match:
        return float(match.group(1).replace(",", "."))
    for word, value in sorted(_AMT_WORDS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?:на|за)\s+{word}(?![а-яa-z])", text):
            return float(value)
    return 0.0


class CryptoTradesMixin:
    def _execute_trade(self, text: str, kind: str, ticker: str | None) -> str:
        with common._TRADE_LOCK:
            return self._execute_trade_locked(text, kind, ticker)

    def _execute_trade_locked(self, text: str, kind: str, ticker: str | None) -> str:
        if not common._voice_trade_enabled():
            return "Сделки крипты выключены."
        amount = _extract_quote(text)
        if kind == "auto":
            return self._trade_auto()
        if not self._api_key:
            return "Без ключа Bybit заявки не выставляю."
        target = self._resolve_ticker(ticker)
        if kind == "allin":
            if not target:
                return "Куда перекладывать? Назови биткоин или эфир."
            return self._trade_all_in(target)
        if not target:
            return "Какую монету? Назови биткоин или эфир."
        if kind == "buy":
            return self._trade_buy(target, amount)
        return self._trade_sell(target, amount)

    def _resolve_ticker(self, ticker: str | None) -> str | None:
        if ticker:
            return ticker.upper()
        if len(self._watchlist) == 1:
            return self._watchlist[0]
        if self._last_tickers:
            return self._last_tickers[-1]
        return None

    def _cash_and_held(self) -> tuple[float, dict[str, dict[str, Any]]]:
        cash, positions = self._wallet()
        held = {item["ticker"]: item for item in positions}
        return cash, held

    def _trade_buy(self, ticker: str, amount: float | None) -> str:
        ticker = ticker.upper()
        cash, _held = self._cash_and_held()
        filters = self._filters(ticker)
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        if amount is None:
            quote = cash
        elif amount <= 0:
            quote = min_amt
        else:
            quote = amount
        quote = min(quote, cash)
        if quote + 1e-9 < min_amt:
            raise RuntimeError("no lots")
        return self._place_order(ticker, "Buy", quote_usdt=quote)

    def _trade_sell(self, ticker: str, amount: float | None) -> str:
        ticker = ticker.upper()
        _cash, held = self._cash_and_held()
        pos = held.get(ticker)
        if not pos:
            raise RuntimeError("no lots")
        price = float(pos["price"] or 0) or self._ticker(ticker)["price"]
        filters = self._filters(ticker)
        held_qty = float(pos["qty"])
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        step = filters["step"]
        if amount is None:
            qty = held_qty
        elif amount <= 0:
            need = min_amt / price if price else 0.0
            qty = max(filters["min_qty"], need)
            qty = float(_qty_str(qty, step, round_up=True) or 0)
        else:
            qty = amount / price if price else 0.0
            qty = float(_qty_str(qty, step) or 0)
        qty = min(qty, held_qty)
        leftover = (held_qty - qty) * price if price else 0.0
        if qty + 1e-12 < held_qty and leftover + 1e-9 < min_amt:
            qty = held_qty
        notional = qty * price if price else 0.0
        if qty <= 0 or (notional + 1e-9 < min_amt and qty + 1e-12 < held_qty):
            raise RuntimeError("no lots")
        return self._place_order(ticker, "Sell", base_qty=qty, price=price)

    def _trade_all_in(self, ticker: str) -> str:
        ticker = ticker.upper()
        parts: list[str] = []
        _cash, held = self._cash_and_held()
        for other, pos in list(held.items()):
            if other == ticker:
                continue
            if self._is_owner_position(other):
                continue
            parts.append(self._place_order(other, "Sell", base_qty=float(pos["qty"]), price=float(pos["price"])))
            time.sleep(0.4)
        time.sleep(0.3)
        self._bust_private_cache()
        return ( " ".join(parts) + " " if parts else "") + self._trade_buy(ticker, None)

    def _place_order(
        self,
        ticker: str,
        side: str,
        quote_usdt: float | None = None,
        base_qty: float | None = None,
        price: float = 0.0,
    ) -> str:
        ticker = ticker.upper()
        filters = self._filters(ticker)
        body: dict[str, Any] = {
            "category": "spot",
            "symbol": self._symbol(ticker),
            "side": side,
            "orderType": "Market",
            "isLeverage": 0,
        }
        px = float(price or 0) or self._ticker(ticker)["price"]
        if side == "Buy":
            amt = float(quote_usdt or 0)
            if amt <= 0:
                raise RuntimeError("no lots")
            body["marketUnit"] = "quoteCoin"
            body["qty"] = f"{amt:.2f}"
            filled_quote = amt
        else:
            qty = float(base_qty or 0)
            qty_s = _qty_str(qty, filters["step"])
            if float(qty_s or 0) <= 0:
                raise RuntimeError("no lots")
            body["marketUnit"] = "baseCoin"
            body["qty"] = qty_s
            filled_quote = float(qty_s) * px
        data = self._signed("POST", "/v5/order/create", body)
        result = data.get("result") or {}
        if not result.get("orderId") and not result.get("orderLinkId"):
            raise RuntimeError(str(data.get("retMsg") or "заявка не прошла"))
        self._bust_private_cache()
        if side == "Buy":
            self._mark_desk_bought(ticker)
        spoken = _spoken(ticker)
        verb = "Купил" if side == "Buy" else "Продал"
        phrase = f"{verb} {spoken} на {_format_usd(filled_quote)}."
        self._journal_trade(ticker, side, filled_quote, px, spoken)
        self._last_tickers = [ticker]
        return phrase
