# skills/crypto.py
# Спот Bybit: курс, портфель, сделки голосом и авто-ребаланс.
# Т-Инвест API спот BTC не даёт — площадка отдельная. Без плеча и фьючерсов.
# ИИ (Groq) выбирает доли монет из ленты Bybit; заявки — только с тумблером.

from __future__ import annotations

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

from skills.base import BaseSkill, RequestContext
from skills.text_utils import has_any_word, has_word, norm as _norm, plural as _plural
from skills.utils import send_telegram_notification, telegram_configured

logger = logging.getLogger(__name__)

_API_PROD = "https://api.bybit.com"
_API_TEST = "https://api-testnet.bybit.com"
_QUOTE = "USDT"
_CACHE_SEC = 25.0
_DESK_PERIOD_SEC = 45 * 60
_DEFAULT_WATCH = ("BTC", "ETH", "SOL")
_STABLES = frozenset({"USDT", "USDC", "DAI", "FDUSD", "USDE"})
_MIN_QUOTE = 5.0
_MIN_TRADE_PCT = 0.02
_SMALL_EQUITY = 50.0
_AI_MAX_NAMES = 4
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOLD_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_holds.json")
_BOUGHT_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_bought.json")
_TRADE_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_trades.json")

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


def _is_encyclopedia(text: str) -> bool:
    return any(phrase in text for phrase in _ENCYCLOPEDIA)


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


def _wants_journal(text: str) -> bool:
    return any(hint in text for hint in _JOURNAL_HINTS)


def _wants_advice(text: str) -> bool:
    return any(hint in text for hint in _ADVICE_HINTS)


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


def _read_trades() -> list[dict[str, Any]]:
    if not os.path.isfile(_TRADE_PATH):
        return []
    try:
        with open(_TRADE_PATH, encoding="utf-8") as handle:
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
    tmp_path = _TRADE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _TRADE_PATH)
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


class CryptoSkill(BaseSkill):
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
        self._manual_holds: set[str] = _read_ticker_set(_HOLD_PATH) or set()
        loaded_bought = _read_ticker_set(_BOUGHT_PATH)
        self._desk_bought: set[str] = loaded_bought or set()
        self._desk_bought_ready = loaded_bought is not None
        self._desk_stop = threading.Event()
        self._desk_enabled = threading.Event()
        self._desk_enabled.set()
        self._desk_thread: threading.Thread | None = None

    def _reload_env(self) -> None:
        self._api_key = (os.getenv("BYBIT_API_KEY") or os.getenv("CRYPTO_API_KEY") or "").strip()
        self._api_secret = (os.getenv("BYBIT_API_SECRET") or os.getenv("CRYPTO_API_SECRET") or "").strip()
        self._watchlist = _watchlist()

    def _host(self) -> str:
        return _API_TEST if _testnet() else _API_PROD

    def _ensure_desk_loop(self) -> None:
        global _desk_loop_started
        with _desk_init_lock:
            if _desk_loop_started:
                return
            self._desk_thread = threading.Thread(
                target=self._desk_loop,
                name="crypto-desk",
                daemon=True,
            )
            self._desk_thread.start()
            _desk_loop_started = True

    def on_disabled(self) -> None:
        self._desk_enabled.clear()

    def on_enabled(self) -> None:
        self._desk_enabled.set()
        self.start_background()

    def start_background(self) -> None:
        self._ensure_desk_loop()

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

    def _run_journal(self, channel: str = "voice") -> str:
        text = format_journal() or "Дневник пуст: сделок ещё не было."
        if channel == "telegram":
            return text
        if telegram_configured():
            send_telegram_notification(text, background=False)
            return "Отправил дневник крипты в телеграм."
        return text

    def _run_advisor(self, channel: str = "voice") -> str:
        import crypto_advisor

        send_tg = telegram_configured() and channel != "telegram"
        text = crypto_advisor.run(skill=self, to_telegram=send_tg, to_stdout=False)
        if channel == "telegram":
            return text
        if telegram_configured():
            return "Отправил совет по крипте в телеграм."
        return text

    def _execute_trade(self, text: str, kind: str, ticker: str | None) -> str:
        with _TRADE_LOCK:
            return self._execute_trade_locked(text, kind, ticker)

    def _execute_trade_locked(self, text: str, kind: str, ticker: str | None) -> str:
        if not _voice_trade_enabled():
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
        if telegram_configured():
            send_telegram_notification(line, background=True)

    def _bust_private_cache(self) -> None:
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if not key.startswith("px:")
        }

    def _mark_desk_bought(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._desk_bought.add(ticker)
        self._desk_bought_ready = True
        _write_ticker_set(_BOUGHT_PATH, self._desk_bought)

    def _ensure_desk_bought(self, held: set[str]) -> None:
        if self._desk_bought_ready:
            return
        self._desk_bought = {ticker for ticker in held if ticker not in self._manual_holds}
        self._desk_bought_ready = True
        _write_ticker_set(_BOUGHT_PATH, self._desk_bought)

    def _is_owner_position(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker in self._manual_holds:
            return True
        return self._desk_bought_ready and ticker not in self._desk_bought

    def desk_candidates(self) -> list[dict[str, Any]]:
        tape = self._tape()
        from_tape = {row["ticker"]: row for row in tape}
        held: dict[str, float] = {}
        if self._api_key:
            try:
                _cash, positions = self._wallet()
                held = {item["ticker"]: item["qty"] for item in positions}
            except Exception as exc:
                logger.warning("[Крипта] кошелёк для стола: %s", exc)
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            ticker = row["ticker"]
            if ticker in seen or ticker in _STABLES:
                return
            seen.add(ticker)
            mom = {"chg_7": None, "trend": "нет данных"}
            try:
                mom = self._momentum(ticker)
            except Exception:
                pass
            chosen.append({**row, "held": held.get(ticker, 0), **mom})

        for ticker in list(held) + list(self._watchlist):
            row = from_tape.get(ticker)
            if row is None:
                try:
                    px = self._ticker(ticker)
                    row = {
                        "ticker": ticker,
                        "name": _spoken(ticker),
                        "price": px["price"],
                        "chg": px["chg"],
                        "turnover": px["turnover"],
                    }
                except Exception:
                    continue
            add(row)
        for row in tape:
            if len(chosen) >= 12:
                break
            add(row)
        return chosen

    def ai_pick_alloc(self, candidates: list[dict[str, Any]]) -> tuple[dict[str, float], str]:
        allowed = {str(row.get("ticker") or "").upper() for row in candidates if row.get("ticker")}
        facts = self._alloc_facts(candidates)
        try:
            from skill_settings import get_effective_groq_model
            from skills.groq_client import complete

            raw = complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты Джарвис. Выбираешь спотовый портфель на Bybit в USDT. "
                            "Без плеча, без фьючерсов, без стейблов в alloc. "
                            "Ответ строго JSON: {\"alloc\": {\"BTC\": 40, \"ETH\": 30}, "
                            "\"why\": \"коротко по-русски до 180 знаков\"}. "
                            "Только тикеры из списка. Сумма до 100, остаток — кэш USDT. "
                            "Если картина плохая — пустой alloc. Не обещай прибыль. Мужской род."
                        ),
                    },
                    {"role": "user", "content": facts + "\n\nВыбери доли."},
                ],
                preferred=get_effective_groq_model(),
                temperature=0.2,
                max_tokens=250,
            )
            alloc, why = parse_ai_alloc(raw, allowed)
            if alloc:
                return alloc, why
        except Exception as exc:
            logger.warning("[Крипта] ИИ-выбор: %s", exc)
        return self._fallback_alloc(candidates), "ИИ смолчал, держу простые доли."

    def _alloc_facts(self, candidates: list[dict[str, Any]]) -> str:
        cash = 0.0
        book = "счёт недоступен"
        if self._api_key:
            try:
                cash, positions = self._wallet()
                parts = [f"кэш {cash:.0f} USDT"]
                for item in positions[:6]:
                    parts.append(f"{item['name']} ({item['ticker']}) {item['value']:.0f}")
                book = "; ".join(parts)
            except Exception:
                book = "счёт недоступен"
        lines = [f"Портфель: {book}", "Кандидаты спот USDT:"]
        for row in candidates[:12]:
            mom = row.get("chg_7")
            mom_s = f", 7д {mom:+.1f}%" if mom is not None else ""
            lines.append(
                f"{row.get('name')} ({row.get('ticker')}) "
                f"{row.get('price')} USDT, сутки {row.get('chg'):+.1f}%{mom_s}, "
                f"оборот {row.get('turnover'):.0f}"
            )
        return "\n".join(lines)

    def _fallback_alloc(self, candidates: list[dict[str, Any]]) -> dict[str, float]:
        picked = [
            str(row.get("ticker") or "").upper()
            for row in candidates
            if row.get("ticker")
            and str(row.get("ticker")).upper() not in _STABLES
            and (row.get("chg_7") is None or float(row.get("chg_7") or 0) >= 0)
        ][:2]
        if not picked:
            watch = [ticker for ticker in self._watchlist if ticker not in _STABLES]
            picked = watch[:1]
        if not picked:
            return {}
        share = round(100.0 / len(picked), 1)
        return {ticker: share for ticker in picked}

    def _trade_auto(self, silent: bool = False) -> str:
        with _TRADE_LOCK:
            return self._trade_auto_locked(silent)

    def _trade_auto_locked(self, silent: bool = False) -> str:
        candidates = self.desk_candidates()
        if not candidates:
            return "Нечего решать: лента пуста."
        alloc, why = self.ai_pick_alloc(candidates)
        if not alloc:
            result = why or "ИИ оставил кэш."
            if silent:
                logger.info("[Крипта] авто: %s", result)
            return result
        desc = ", ".join(f"{_spoken(ticker)} {int(round(pct))}%" for ticker, pct in alloc.items())
        logger.info("[Крипта] целевой портфель: %s (%s)", desc, why)
        if not self._api_key:
            return f"Целевой портфель: {desc}. Ключа Bybit нет. {why}".strip()
        result = self._rebalance(alloc)
        if why and not silent:
            return f"{result} {why}".strip()
        return result

    def _rebalance(self, target_alloc: dict[str, float]) -> str:
        cash, positions_list = self._wallet()
        positions = {item["ticker"]: item for item in positions_list}
        self._ensure_desk_bought(set(positions.keys()))
        equity = max(cash + sum(item["value"] for item in positions_list), 1.0)
        min_trade = _min_trade_usd(equity)
        prices = {item["ticker"]: float(item["price"] or 0) for item in positions_list}
        for ticker in set(target_alloc) | set(positions):
            if ticker not in prices or prices[ticker] <= 0:
                try:
                    prices[ticker] = self._ticker(ticker)["price"]
                except Exception:
                    prices[ticker] = 0.0
        relevant = set(positions) | set(target_alloc)
        target_values = {ticker: equity * (target_alloc.get(ticker, 0.0) / 100.0) for ticker in relevant}
        current_values = {
            ticker: float(positions.get(ticker, {}).get("value") or 0)
            for ticker in relevant
        }
        parts: list[str] = []
        sells = [
            (ticker, current_values[ticker] - target_values[ticker])
            for ticker in relevant
            if current_values[ticker] - target_values[ticker] >= min_trade
        ]
        sells.sort(key=lambda item: item[1], reverse=True)
        for ticker, excess in sells:
            if target_alloc.get(ticker, 0.0) <= 0 and self._is_owner_position(ticker):
                continue
            price = prices.get(ticker) or 0.0
            qty = float(positions.get(ticker, {}).get("qty") or 0)
            if target_alloc.get(ticker, 0.0) <= 0:
                sell_qty = qty
            else:
                sell_qty = excess / price if price else 0.0
            sell_qty = min(sell_qty, qty)
            if sell_qty <= 0:
                continue
            try:
                parts.append(self._place_order(ticker, "Sell", base_qty=sell_qty, price=price))
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] продажа %s: %s", ticker, exc)
        time.sleep(0.3)
        self._bust_private_cache()
        cash, _pos = self._cash_and_held()
        buys = [
            (ticker, target_values.get(ticker, 0) - current_values.get(ticker, 0))
            for ticker in target_alloc
            if target_values.get(ticker, 0) - current_values.get(ticker, 0) >= min_trade
        ]
        buys.sort(key=lambda item: item[1], reverse=True)
        for ticker, need in buys:
            quote = min(need, cash)
            if quote < _MIN_QUOTE:
                continue
            try:
                parts.append(self._place_order(ticker, "Buy", quote_usdt=quote))
                cash -= quote
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] покупка %s: %s", ticker, exc)
        if not parts:
            return "Портфель уже близко к цели."
        return " ".join(parts)

    def _desk_ready(self) -> bool:
        if not self._desk_enabled.is_set():
            return False
        if not _auto_trade_enabled():
            return False
        try:
            from skill_settings import is_skill_enabled
            if not is_skill_enabled(self):
                self._desk_enabled.clear()
                return False
        except Exception:
            pass
        return True

    def _desk_loop(self) -> None:
        self._desk_stop.wait(90)
        while not self._desk_stop.is_set():
            if not self._desk_ready():
                if self._desk_stop.wait(2.0):
                    return
                continue
            deadline = time.time() + _DESK_PERIOD_SEC
            skipped = False
            while time.time() < deadline:
                remaining = min(2.0, deadline - time.time())
                if remaining <= 0:
                    break
                if self._desk_stop.wait(remaining):
                    return
                if not self._desk_ready():
                    skipped = True
                    break
            if skipped:
                continue
            try:
                self._reload_env()
                self._trade_auto(silent=True)
            except Exception as exc:
                logger.warning("[Крипта] фоновый цикл: %s", exc)
