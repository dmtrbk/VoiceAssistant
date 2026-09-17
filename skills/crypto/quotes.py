# skills/crypto/quotes.py
# Курс, лента, кошелёк и голосовые сводки.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import requests

from skills.text_utils import has_any_word, has_word, norm as _norm
from . import common
from .common import (
    _ADVICE_HINTS,
    _ALLIN_HINTS,
    _API_PROD,
    _API_TEST,
    _AUTO_HINTS,
    _BUY_HINTS,
    _CACHE_SEC,
    _COINS,
    _ENCYCLOPEDIA,
    _MIN_QUOTE,
    _PRICE_HINTS,
    _QUOTE,
    _SELL_HINTS,
    _STABLES,
    _STATUS_ASK,
    _TOPIC_WORDS,
    _format_pct,
    _format_rub,
    _format_usd,
    _leveraged,
    _spoken,
    _testnet,
    _watchlist,
)

logger = logging.getLogger(__name__)


def _is_encyclopedia(text: str) -> bool:
    return any(phrase in text for phrase in _ENCYCLOPEDIA)


_IDENTITY_TALK = (
    "зачем",
    "почему ты",
    "почему тебе",
    "для чего",
    "любишь",
    "предпочита",
    "смысл крипт",
    "зачем тебе",
    "зачем ты",
)


def _is_identity_talk(text: str) -> bool:
    return any(hint in text for hint in _IDENTITY_TALK)


def _has_topic(text: str) -> bool:
    if "криптовалют" in text:
        return True
    return has_any_word(text, _TOPIC_WORDS)


def _coin_hits(text: str) -> list[str]:
    found: list[str] = []
    for ticker, meta in _COINS.items():
        if has_any_word(text, tuple(meta["words"])) or has_any_word(text, tuple(meta["short"])):
            found.append(ticker)
    return found


def _unambiguous_coin(text: str, ticker: str) -> bool:
    meta = _COINS[ticker]
    ambiguous = meta["ambiguous"]
    if has_any_word(text, tuple(meta["short"])):
        return True
    hits = [word for word in meta["words"] if has_word(text, word)]
    if not hits:
        return False
    return any(word not in ambiguous for word in hits)


def is_crypto_command(text: str) -> bool:
    """Узкий перехват: курс, портфель и сделки крипты, не радиоэфир и не «что такое»."""
    text = _norm(text)
    if not text or _is_encyclopedia(text):
        return False
    if _is_identity_talk(text):
        has_action = any(
            hint in text
            for hint in (
                *_PRICE_HINTS,
                *_BUY_HINTS,
                *_SELL_HINTS,
                *_AUTO_HINTS,
                *_ALLIN_HINTS,
                *_ADVICE_HINTS,
                *_STATUS_ASK,
            )
        )
        if not has_action:
            return False
    coins = _coin_hits(text)
    topic = _has_topic(text)
    if not coins and not topic:
        return False
    if topic:
        return True
    if any(_unambiguous_coin(text, ticker) for ticker in coins):
        return True
    if any(hint in text for hint in _PRICE_HINTS):
        return True
    if any(hint in text for hint in (*_BUY_HINTS, *_SELL_HINTS, *_AUTO_HINTS, *_ALLIN_HINTS)):
        return True
    if any(hint in text for hint in _ADVICE_HINTS):
        return True
    if any(ask in text for ask in _STATUS_ASK):
        return True
    return False


class CryptoQuotesMixin:
    def _reload_env(self) -> None:
        self._api_key = (os.getenv("BYBIT_API_KEY") or os.getenv("CRYPTO_API_KEY") or "").strip()
        self._api_secret = (os.getenv("BYBIT_API_SECRET") or os.getenv("CRYPTO_API_SECRET") or "").strip()
        self._watchlist = _watchlist()

    def _host(self) -> str:
        return _API_TEST if _testnet() else _API_PROD
    def _public_get(self, path: str, params: dict[str, str], cache_key: str) -> dict[str, Any]:
        hit = self._cache.get(cache_key)
        if hit and time.time() - hit[0] < _CACHE_SEC:
            return hit[1]
        response = self._session.get(
            f"{self._host()}{path}",
            params=params,
            timeout=6,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"bybit {response.status_code}")
        data = response.json() if response.content else {}
        if int(data.get("retCode") or 0) != 0:
            raise RuntimeError(str(data.get("retMsg") or "bybit error"))
        self._cache[cache_key] = (time.time(), data)
        return data

    def _signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api_key or not self._api_secret:
            raise RuntimeError("token rejected")
        params = dict(params or {})
        timestamp = str(int(time.time() * 1000))
        recv = "10000"
        if method == "GET":
            payload = "&".join(f"{key}={params[key]}" for key in sorted(params))
            body = None
        else:
            payload = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
            body = payload
        sign = hmac.new(
            self._api_secret.encode(),
            f"{timestamp}{self._api_key}{recv}{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }
        url = f"{self._host()}{path}"
        if method == "GET":
            if payload:
                url = f"{url}?{payload}"
            response = self._session.get(url, headers=headers, timeout=8)
        else:
            response = self._session.post(url, data=body, headers=headers, timeout=8)
        if response.status_code in {401, 403}:
            raise RuntimeError("token rejected")
        if response.status_code >= 400:
            snippet = (response.text or "")[:200].lower()
            if any(mark in snippet for mark in ("permission", "api key", "invalid", "denied")):
                raise RuntimeError("trade token")
            raise RuntimeError(f"bybit {response.status_code}")
        data = response.json() if response.content else {}
        code = int(data.get("retCode") or 0)
        if code != 0:
            msg = str(data.get("retMsg") or "")
            low = msg.lower()
            if any(mark in low for mark in ("api key", "invalid", "denied", "timestamp")):
                raise RuntimeError("token rejected")
            if any(mark in low for mark in ("insufficient", "not enough", "ab not enough")):
                raise RuntimeError("no lots")
            if any(mark in low for mark in ("lower limit", "min order", "order value", "too small")):
                raise RuntimeError("no lots")
            raise RuntimeError(msg or "bybit error")
        return data

    def _symbol(self, ticker: str) -> str:
        return f"{ticker.upper()}{_QUOTE}"

    def _ticker(self, ticker: str) -> dict[str, float]:
        ticker = ticker.upper()
        data = self._public_get(
            "/v5/market/tickers",
            {"category": "spot", "symbol": self._symbol(ticker)},
            f"px:{ticker}",
        )
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            raise RuntimeError("unknown coin")
        row = rows[0]
        last = float(row.get("lastPrice") or 0)
        if last <= 0:
            raise RuntimeError("unknown coin")
        chg = float(row.get("price24hPcnt") or 0) * 100.0
        turnover = float(row.get("turnover24h") or 0)
        return {"price": last, "chg": chg, "turnover": turnover}

    def _tape(self) -> list[dict[str, Any]]:
        data = self._public_get(
            "/v5/market/tickers",
            {"category": "spot"},
            "px:spot:all",
        )
        tape: list[dict[str, Any]] = []
        for row in (data.get("result") or {}).get("list") or []:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol.endswith(_QUOTE) or _leveraged(symbol):
                continue
            base = symbol[: -len(_QUOTE)]
            if not base or base in _STABLES:
                continue
            last = float(row.get("lastPrice") or 0)
            if last <= 0:
                continue
            tape.append(
                {
                    "ticker": base,
                    "name": _spoken(base),
                    "price": last,
                    "chg": float(row.get("price24hPcnt") or 0) * 100.0,
                    "turnover": float(row.get("turnover24h") or 0),
                }
            )
        tape.sort(key=lambda item: item["turnover"], reverse=True)
        return tape

    def _filters(self, ticker: str) -> dict[str, float]:
        ticker = ticker.upper()
        data = self._public_get(
            "/v5/market/instruments-info",
            {"category": "spot", "symbol": self._symbol(ticker)},
            f"inst:{ticker}",
        )
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            return {"min_qty": 0.0, "min_amt": _MIN_QUOTE, "step": 0.0}
        lot = rows[0].get("lotSizeFilter") or {}
        return {
            "min_qty": float(lot.get("minOrderQty") or 0),
            "min_amt": float(lot.get("minOrderAmt") or _MIN_QUOTE),
            "step": float(lot.get("qtyStep") or lot.get("basePrecision") or 0),
        }

    def _momentum(self, ticker: str) -> dict[str, Any]:
        data = self._public_get(
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": self._symbol(ticker),
                "interval": "D",
                "limit": "8",
            },
            f"kl:{ticker}",
        )
        rows = (data.get("result") or {}).get("list") or []
        closes = [float(item[4]) for item in rows if len(item) > 4]
        if len(closes) < 2:
            return {"chg_7": None, "trend": "нет данных"}
        now, old = closes[0], closes[-1]
        if old <= 0:
            return {"chg_7": None, "trend": "нет данных"}
        chg = (now - old) / old * 100.0
        return {"chg_7": chg, "trend": "выше" if chg >= 0 else "ниже"}

    def _wallet(self) -> tuple[float, list[dict[str, Any]]]:
        data = self._signed("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        accounts = (data.get("result") or {}).get("list") or []
        coins = (accounts[0].get("coin") or []) if accounts else []
        cash = 0.0
        positions: list[dict[str, Any]] = []
        for row in coins:
            coin = str(row.get("coin") or "").upper()
            qty = float(row.get("walletBalance") or row.get("equity") or 0)
            usd = float(row.get("usdValue") or 0)
            if coin in _STABLES:
                cash += usd if usd else qty
                continue
            if qty <= 0:
                continue
            price = usd / qty if qty else 0.0
            positions.append(
                {
                    "ticker": coin,
                    "name": _spoken(coin),
                    "qty": qty,
                    "price": price,
                    "value": usd,
                }
            )
        positions.sort(key=lambda item: item["value"], reverse=True)
        return cash, positions

    def _speak_one(self, ticker: str) -> str:
        row = self._ticker(ticker)
        speech = f"{_spoken(ticker)} {_format_usd(row['price'])}, {_format_pct(row['chg'])} за сутки"
        if self._want_rub:
            usdt_rub = 0.0
            try:
                data = self._public_get(
                    "/v5/market/tickers",
                    {"category": "spot", "symbol": "USDTRUB"},
                    "px:USDTRUB",
                )
                rows = (data.get("result") or {}).get("list") or []
                if rows:
                    usdt_rub = float(rows[0].get("lastPrice") or 0)
            except Exception:
                usdt_rub = 0.0
            if usdt_rub > 0:
                speech += f", это {_format_rub(row['price'] * usdt_rub)}"
        return speech + "."

    def _speak_watch(self, emphasize_yield: bool) -> str:
        parts: list[str] = []
        for ticker in self._watchlist[:4]:
            try:
                row = self._ticker(ticker)
            except Exception:
                continue
            parts.append(f"{_spoken(ticker)} {_format_usd(row['price'])}, {_format_pct(row['chg'])}")
            self._last_tickers.append(ticker)
        if not parts:
            return "Курс сейчас не отвечает."
        speech = ". ".join(parts) + "."
        if emphasize_yield:
            speech += " Это за сутки, без учёта твоего счёта."
        self._last_tickers = self._last_tickers[-8:]
        return speech

    def _speak_portfolio(self, emphasize_yield: bool) -> str:
        cash, positions = self._wallet()
        if not positions:
            watch = self._speak_watch(False)
            cash_bit = f" Кэш {_format_usd(cash)}." if cash else ""
            return f"Монет на счёте нет.{cash_bit} {watch}".strip()
        self._last_tickers = [item["ticker"] for item in positions[:4]]
        lines = [
            f"{item['name']} {_format_usd(item['value'])}"
            for item in positions[:4]
        ]
        speech = ". ".join(lines) + "."
        if cash:
            speech += f" Кэш {_format_usd(cash)}."
        if emphasize_yield:
            total = cash + sum(item["value"] for item in positions)
            speech += f" Всего {_format_usd(total)}."
        return speech
