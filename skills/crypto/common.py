# skills/crypto/common.py
# Общие константы и помощники спота Bybit.


import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from skills.text_utils import plural as _plural
from skills.utils import send_telegram_notification, telegram_configured

logger = logging.getLogger(__name__)

_API_PROD = "https://api.bybit.com"
_API_TEST = "https://api-testnet.bybit.com"
_QUOTE = "USDT"
_CACHE_SEC = 25.0
_DESK_PERIOD_SEC = 6 * 60 * 60  # полный скор + ребаланс
_WATCH_PERIOD_SEC = 12 * 60  # дозор: TP / risk-off / добор ядра к сохранённой цели
_DEFAULT_WATCH = ("BTC", "ETH", "SOL")
_STABLES = frozenset({"USDT", "USDC", "DAI", "FDUSD", "USDE"})
_MIN_QUOTE = 5.0
_MIN_TRADE_PCT = 0.02
_SMALL_EQUITY = 50.0
_AI_MAX_NAMES = 4
# Запас под комиссию/пыль при market buy «на весь кэш» или хвост ребаланса.
_BUY_CASH_BUFFER = 0.998
_TRADE_HISTORY_KEEP = 5000
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOLD_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_holds.json")
_BOUGHT_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_bought.json")
_TRADE_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_trades.json")
_DAILY_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_daily.json")
_ALLOC_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_alloc.json")

_ENCYCLOPEDIA = (
    "что такое", "что значит", "кто такой", "кто такая",
    "расскажи про", "объясни что",
)
_PRICE_HINTS = (
    "сколько стоит", "какая цена", "цена на", "курс",
    "как там", "что там", "что со ", "что с ", "котиров",
)
_STATUS_ASK = (
    "что там", "как там", "покажи", "скажи", "сколько",
    "какой ", "какая ", "какие ", "что с ", "что со ",
    "состояние", "сводка", "котиров", "курс",
)
_YIELD_HINTS = (
    "в плюсе", "в минусе", "прибыл", "убыт", "заработал",
    "доход", "сколько я", "результат",
)
_BUY_HINTS = ("купи", "докупи", "возьми")
_SELL_HINTS = ("продай",)
_AUTO_HINTS = ("поторгуй", "поторгуйся", "сыграй на бирже")
_ALLIN_HINTS = ("переложи", "вложи все", "вложи всё", "все в ", "всё в ")
_MAX_HINTS = ("все", "всё", "весь", "всю", "целиком", "максимум", "полностью")
_ADVICE_HINTS = (
    "посоветуй", "рекомендаци", "какие монеты", "что купить",
    "разбери сигнал", "пришли совет",
)
_JOURNAL_HINTS = ("дневник", "журнал сделок")
_FOLLOWUP = (
    "подробнее", "подробней", "первая", "вторая",
    "ещё", "еще", "доллар", "в долларах",
)
_TOPIC_WORDS = ("крипта", "крипте", "криптой", "крипту", "крипты")
_AMT_WORDS = {
    "один": 1, "одна": 1, "одно": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "пятнадцать": 15, "двадцать": 20, "тридцать": 30,
    "пятьдесят": 50, "сто": 100, "двести": 200, "пятьсот": 500,
}

_COINS: dict[str, dict[str, Any]] = {
    "BTC": {
        "spoken": "Биткоин",
        "words": (
            "биткоин", "биткойн", "биток", "битка",
            "биткоина", "биткоине", "биткоину", "биткоином", "биткоины",
            "bitcoin",
        ),
        "short": ("btc", "бтц"),
        "ambiguous": frozenset({"биток", "битка"}),
    },
    "ETH": {
        "spoken": "Эфир",
        "words": (
            "эфир", "эфира", "эфире", "эфиру", "эфиром",
            "эфириум", "эфириума", "эфириуме", "ethereum",
        ),
        "short": ("eth", "этх"),
        "ambiguous": frozenset({"эфир", "эфира", "эфире", "эфиру", "эфиром"}),
    },
    "SOL": {
        "spoken": "Солана",
        "words": ("солана", "соланы", "солану", "солане", "solana"),
        "short": ("sol",),
        "ambiguous": frozenset(),
    },
    "TON": {
        "spoken": "Тон",
        "words": ("тонкоин", "тон", "toncoin"),
        "short": ("ton",),
        "ambiguous": frozenset({"тон"}),
    },
    "XRP": {
        "spoken": "Рипл",
        "words": ("рипл", "рипла", "ripple"),
        "short": ("xrp",),
        "ambiguous": frozenset(),
    },
    "DOGE": {
        "spoken": "Дож",
        "words": ("дож", "догикоин", "dogecoin"),
        "short": ("doge",),
        "ambiguous": frozenset(),
    },
}

_GECKO_IDS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "toncoin": "TON", "ripple": "XRP", "dogecoin": "DOGE",
}

_desk_loop_started = False
_desk_init_lock = threading.Lock()
_TRADE_LOCK = threading.RLock()
def _format_pct(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - int(rounded)) < 0.05:
        shown = str(int(round(rounded)))
    else:
        shown = f"{rounded:.1f}".replace(".", ",")
    if rounded > 0:
        return f"плюс {shown} процента"
    if rounded < 0:
        return f"минус {shown.replace('-', '')} процента"
    return "без изменения"


def _format_usd(amount: float, signed: bool = False) -> str:
    rounded = int(round(amount))
    word = _plural(abs(rounded), "доллар", "доллара", "долларов")
    if signed:
        if rounded > 0:
            return f"плюс {rounded} {word}"
        if rounded < 0:
            return f"минус {abs(rounded)} {word}"
        return f"0 {word}"
    if abs(amount) >= 1_000_000:
        mln = amount / 1_000_000.0
        shown = f"{mln:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{shown} млн долларов"
    return f"{rounded} {word}"


def _format_rub(amount: float) -> str:
    if amount >= 1_000_000:
        mln = amount / 1_000_000.0
        shown = f"{mln:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{shown} млн рублей"
    rounded = int(round(amount))
    word = _plural(abs(rounded), "рубль", "рубля", "рублей")
    return f"{rounded} {word}"


def _qty_str(value: float, step: float, *, round_up: bool = False) -> str:
    step = float(step or 0)
    if step > 0:
        units = value / step
        if round_up:
            value = math.ceil(units - 1e-12) * step
        else:
            value = math.floor(units + 1e-12) * step
    if step >= 1:
        return str(int(value))
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _testnet() -> bool:
    return (os.getenv("BYBIT_TESTNET") or os.getenv("CRYPTO_TESTNET") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _voice_trade_enabled() -> bool:
    try:
        from skill_settings import is_crypto_voice_trade_enabled
        return is_crypto_voice_trade_enabled()
    except Exception:
        pass
    raw = (os.getenv("CRYPTO_VOICE_TRADE") or os.getenv("BYBIT_VOICE_TRADE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _auto_trade_enabled() -> bool:
    try:
        from skill_settings import is_crypto_auto_trade_enabled
        return is_crypto_auto_trade_enabled()
    except Exception:
        pass
    raw = (os.getenv("CRYPTO_AUTO_TRADE") or os.getenv("BYBIT_AUTO_TRADE") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return _testnet()

def _normalize_ticker(raw: str) -> str:
    token = (raw or "").strip().upper()
    mapped = _GECKO_IDS.get(token.lower(), "")
    if mapped:
        return mapped
    if token.endswith(_QUOTE) and len(token) > 4:
        token = token[: -len(_QUOTE)]
    return token


def _watchlist() -> list[str]:
    raw = (os.getenv("CRYPTO_WATCHLIST") or "").strip()
    if not raw:
        return list(_DEFAULT_WATCH)
    ids: list[str] = []
    for part in raw.split(","):
        ticker = _normalize_ticker(part)
        if ticker and ticker not in _STABLES and ticker not in ids:
            ids.append(ticker)
    return ids or list(_DEFAULT_WATCH)


def _spoken(ticker: str) -> str:
    meta = _COINS.get(ticker.upper())
    if meta:
        return str(meta["spoken"])
    return ticker.upper()


def _read_ticker_set(path: str) -> set[str] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return set()
    if isinstance(raw, dict):
        raw = raw.get("tickers") or []
    if not isinstance(raw, list):
        return set()
    return {_normalize_ticker(str(item)) for item in raw if str(item).strip()}


def _write_ticker_set(path: str, tickers: set[str]) -> None:
    payload = {"tickers": sorted(tickers)}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] не записал %s: %s", os.path.basename(path), exc)


def read_alloc_state(path: str | None = None) -> dict[str, Any] | None:
    """Последняя цель стола + метки дозора. None — файла нет / битый."""
    path = path or _ALLOC_PATH
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    alloc_raw = raw.get("alloc") or {}
    if not isinstance(alloc_raw, dict):
        alloc_raw = {}
    alloc: dict[str, float] = {}
    for key, value in alloc_raw.items():
        ticker = _normalize_ticker(str(key))
        if not ticker or ticker in _STABLES:
            continue
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            alloc[ticker] = pct
    watch_trades: list[float] = []
    for item in raw.get("watch_trades") or []:
        try:
            watch_trades.append(float(item))
        except (TypeError, ValueError):
            continue
    return {
        "alloc": alloc,
        "why": str(raw.get("why") or ""),
        "ts": float(raw.get("ts") or 0),
        "watch_trades": watch_trades,
    }


def write_alloc_state(
    alloc: dict[str, float],
    *,
    why: str = "",
    watch_trades: list[float] | None = None,
    path: str | None = None,
) -> None:
    path = path or _ALLOC_PATH
    clean = {
        _normalize_ticker(str(k)): float(v)
        for k, v in (alloc or {}).items()
        if _normalize_ticker(str(k)) and float(v) > 0
    }
    payload = {
        "alloc": clean,
        "why": str(why or ""),
        "ts": time.time(),
        "watch_trades": list(watch_trades or [])[-20:],
    }
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] не записал цель стола: %s", exc)

def _extract_json_object(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for index, char in enumerate(raw[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(raw[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def parse_ai_alloc(raw: str, allowed: set[str]) -> tuple[dict[str, float], str]:
    """Разбор JSON ИИ. Только тикеры из allowed. why — короткий текст."""
    payload = _extract_json_object(raw or "") or {}
    why = str(payload.get("why") or "").strip()
    alloc_raw = payload.get("alloc")
    if not isinstance(alloc_raw, dict):
        alloc_raw = {key: val for key, val in payload.items() if key != "why"}
    cleaned: dict[str, float] = {}
    for key, val in alloc_raw.items():
        ticker = _normalize_ticker(str(key))
        if ticker not in allowed or ticker in _STABLES:
            continue
        try:
            pct = float(val)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            cleaned[ticker] = pct
    if not cleaned:
        return {}, why
    total = sum(cleaned.values())
    if total <= 0:
        return {}, why
    return {key: round((val / total) * 100.0, 1) for key, val in cleaned.items()}, why


def _min_trade_usd(equity: float) -> float:
    return max(equity * _MIN_TRADE_PCT, _MIN_QUOTE)


def _leveraged(symbol: str) -> bool:
    upper = symbol.upper()
    return upper.endswith(("UP", "DOWN", "3L", "3S", "5L", "5S"))
