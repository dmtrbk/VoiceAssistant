# skills/crypto/skill.py
# Навык крипты: маршрутизация голоса по курсу, сделкам, столу и журналу.

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import requests

from skills.base import BaseSkill, RequestContext
from skills.text_utils import norm as _norm
from . import common
from .common import _DEFAULT_WATCH, _FOLLOWUP, _YIELD_HINTS, _read_ticker_set
from .desk import CryptoDeskMixin, _wants_advice
from .journal import CryptoJournalMixin, _wants_journal
from .quotes import CryptoQuotesMixin, _coin_hits, is_crypto_command
from .trades import CryptoTradesMixin, _trade_kind

logger = logging.getLogger(__name__)


class CryptoSkill(
    CryptoQuotesMixin,
    CryptoTradesMixin,
    CryptoDeskMixin,
    CryptoJournalMixin,
    BaseSkill,
):
    """Спот Bybit: курс без ключа, сделки и авто — с ключом и тумблерами."""

    def __init__(self) -> None:
        self._api_key = ""
        self._api_secret = ""
        self._watchlist = list(_DEFAULT_WATCH)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "VoiceAssistant/1.0"})
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_tickers: list[str] = []
        self._want_rub = False
        self._manual_holds: set[str] = _read_ticker_set(common._HOLD_PATH) or set()
        loaded_bought = _read_ticker_set(common._BOUGHT_PATH)
        self._desk_bought: set[str] = loaded_bought or set()
        self._desk_bought_ready = loaded_bought is not None
        self._desk_stop = threading.Event()
        self._desk_enabled = threading.Event()
        self._desk_enabled.set()
        self._desk_thread: threading.Thread | None = None

    def can_handle(self, context: RequestContext) -> bool:
        return is_crypto_command(context.raw_text)

    def accepts_followup(self, context: RequestContext) -> bool:
        if not self._last_tickers:
            return False
        text = _norm(context.raw_text)
        words = text.split()
        if not words or len(words) > 6:
            return False
        if any(marker in text for marker in _FOLLOWUP):
            return True
        return bool(_coin_hits(text))

    def execute(self, context: RequestContext) -> None:
        speak = context.speak
        if speak is None:
            return
        text = _norm(context.raw_text)
        self._reload_env()
        coins = _coin_hits(text)
        if "первая" in text and self._last_tickers:
            coins = self._last_tickers[:1]
        elif "вторая" in text and len(self._last_tickers) >= 2:
            coins = self._last_tickers[1:2]
        ticker = coins[0] if coins else None
        want_yield = any(hint in text for hint in _YIELD_HINTS)
        self._want_rub = any(word in text for word in ("рубл", "подробнее", "подробней"))
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
                self._last_tickers = [ticker]
            elif self._api_key:
                reply = self._speak_portfolio(emphasize_yield=want_yield)
            else:
                reply = self._speak_watch(emphasize_yield=want_yield)
        except requests.Timeout:
            speak("Крипта молчит.")
            return
        except RuntimeError as exc:
            msg = str(exc)
            if "token rejected" in msg:
                speak("Ключ Bybit не подошёл.")
                return
            if "trade token" in msg:
                speak("Нет прав.")
                return
            if "no lots" in msg:
                speak("Не хватает.")
                return
            if "unknown coin" in msg:
                speak("Эту монету не знаю. Назови биткоин или эфир.")
                return
            logger.error("[Крипта] %s", exc)
            speak("Биржа молчит.")
            return
        except Exception as exc:
            logger.error("[Крипта] %s", exc)
            speak("Не вышло.")
            return
        speak(reply)
