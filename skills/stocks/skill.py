# skills/stocks/skill.py
# Навык биржи: маршрутизация голоса по котировкам, сделкам, столу и журналу.

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

import requests

from skills.base import BaseSkill, RequestContext
from skills.text_utils import norm as _norm
from . import common
from .common import (
    _FOLLOWUP,
    _MARKET_WORDS,
    _PRICE_HINTS,
    _YIELD_HINTS,
    _read_ticker_set,
)
from .desk import StocksDeskMixin
from .journal import StocksJournalMixin, _wants_journal
from .quotes import StocksQuotesMixin, _wants_market_report
from .trades import StocksTradesMixin, _trade_kind, _wants_advice

logger = logging.getLogger(__name__)


class StocksSkill(
    StocksQuotesMixin,
    StocksTradesMixin,
    StocksDeskMixin,
    StocksJournalMixin,
    BaseSkill,
):
    """Брокерский счёт Т-Инвест: котировки, портфель, сделки и авто-ребалансировка."""

    def __init__(self) -> None:
        self._token = (os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN") or "").strip()
        self._account_id = (os.getenv("TINKOFF_ACCOUNT_ID") or "").strip()
        raw_watch = (os.getenv("TINKOFF_WATCHLIST") or "").strip()
        self._watchlist = [part.strip().upper() for part in raw_watch.split(",") if part.strip()]
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_tickers: list[str] = []
        self._alias_extra: dict[str, str] = {}
        self._buy_blocked: set[str] = set()
        self._manual_holds: set[str] = _read_ticker_set(common._HOLD_PATH) or set()
        self._manual_tips_sent: set[str] = set(self._manual_holds)
        loaded_bought = _read_ticker_set(common._BOUGHT_PATH)
        self._desk_bought: set[str] = loaded_bought or set()
        self._desk_bought_ready = loaded_bought is not None
        self._desk_stop = threading.Event()
        self._desk_enabled = threading.Event()
        self._desk_enabled.set()
        self._desk_thread: threading.Thread | None = None

    def can_handle(self, context: RequestContext) -> bool:
        text = _norm(context.raw_text)
        if not text:
            return False
        from skills.crypto import is_crypto_command
        if is_crypto_command(text):
            return False
        if _wants_journal(text) or _wants_advice(text):
            return True
        kind = _trade_kind(text)
        if kind == "auto":
            return True
        ticker = self._ticker_from_text(text)
        if kind and (
            ticker
            or any(word in text for word in _MARKET_WORDS)
            or any(word in text for word in ("втб", "фонд", "бирж", "акци", "тмос", "крупнейш"))
        ):
            return True
        if ticker and any(hint in text for hint in _PRICE_HINTS):
            return True
        if _wants_market_report(text):
            return True
        if any(word in text for word in _YIELD_HINTS) and any(
            word in text for word in ("акци", "портфел", "бирж", "бумаг", "счет", "сбер", "газпром", "втб", "фонд", "тмос")
        ) and len(text.split()) <= 10:
            return True
        return False

    def accepts_followup(self, context: RequestContext) -> bool:
        text = _norm(context.raw_text)
        words = re.findall(r"[а-яa-z0-9\-]+", text)
        if not words or len(words) > 6:
            return False
        if any(marker in text for marker in _FOLLOWUP):
            return True
        return self._ticker_from_text(text) is not None

    def execute(self, context: RequestContext) -> None:
        text = _norm(context.raw_text)
        self._token = (os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN") or "").strip()
        self._account_id = (os.getenv("TINKOFF_ACCOUNT_ID") or "").strip()
        raw_watch = (os.getenv("TINKOFF_WATCHLIST") or "").strip()
        self._watchlist = [part.strip().upper() for part in raw_watch.split(",") if part.strip()]
        ticker = self._ticker_from_text(text)
        want_yield = any(hint in text for hint in _YIELD_HINTS)
        kind = _trade_kind(text)

        try:
            if _wants_journal(text):
                reply = self._run_journal(context.channel)
            elif _wants_advice(text):
                reply = self._run_advisor(context.channel)
            elif kind:
                reply = self._execute_trade(text, kind, ticker)
            elif ticker:
                reply = self._speak_one(ticker)
            elif self._token:
                reply = self._speak_portfolio(emphasize_yield=want_yield)
            else:
                reply = self._speak_watch(emphasize_yield=want_yield)
        except requests.Timeout:
            context.speak("Биржа молчит.")
            return
        except RuntimeError as exc:
            msg = str(exc)
            if "token rejected" in msg:
                context.speak("Ключ не подошёл.")
                return
            if "trade token" in msg:
                context.speak("Нет прав.")
                return
            if "market closed" in msg:
                context.speak("Биржа закрыта.")
                return
            if "no lots" in msg:
                context.speak("Не хватает.")
                return
            if "api forbidden" in msg:
                context.speak("Брокер эту бумагу через API не берёт.")
                return
            if "no moex quote" in msg:
                context.speak("Не нашёл.")
                return
            logger.error("[Биржа] Ошибка запроса: %s", exc)
            context.speak("Биржа молчит.")
            return
        except Exception as exc:
            logger.error("[Биржа] Ошибка запроса: %s", exc)
            context.speak("Не вышло.")
            return

        context.speak(reply)
