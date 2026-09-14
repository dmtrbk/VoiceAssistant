# skills/stocks.py
# Брокерский счёт Т-Инвест: котировки Мосбиржи, портфель, сделки и авто-ребалансировка.
# Токен API — только локальный .env, никогда не в git.
# Без ключа — котировки с публичного ISS Мосбиржи; quiet_book.json (не в git)
# держит локальную книжку. Прибыль идёт в фонд модернизации Джарвиса:
# подписки на продвинутые модели ИИ и новое железо.
# Сделки: целевой портфель из активных сигналов Т-Инвест, заявки — код.

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from skills.base import BaseSkill, RequestContext
from skills.text_utils import norm as _norm, plural as _plural
from skills.utils import send_telegram_notification, telegram_configured

logger = logging.getLogger(__name__)

_API_PROD = "https://invest-public-api.tinkoff.ru/rest"
_API_SANDBOX = "https://sandbox-invest-public-api.tinkoff.ru/rest"
_USERS = "UsersService/GetAccounts"
_PORTFOLIO = "OperationsService/GetPortfolio"
_PRICES = "MarketDataService/GetLastPrices"
_FIND = "InstrumentsService/FindInstrument"
_BY_ID = "InstrumentsService/GetInstrumentBy"
_ORDERS = "OrdersService/PostOrder"
_MAX_LOTS = "OrdersService/GetMaxLots"
_SIGNALS = "SignalService/GetSignals"
_MOEX = "https://iss.moex.com/iss/engines/stock/markets/shares"
_MOEX_BOARDS = ("TQBR", "TQTF", "TQPI")
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOK_PATH = os.path.join(_PROJECT_DIR, "quiet_book.json")
_RU_CA = os.path.join(_PROJECT_DIR, "certs", "russian_trusted_root_ca.pem")
_DESK_PERIOD_SEC = 45 * 60
_MOMENTUM_SPREAD = 0.8

_CACHE_SEC = 25.0
_MOOD_TTL_SEC = 3600.0
_MIN_TRADE_PCT = 0.02
_MIN_TRADE_SMALL_PCT = 0.05
_MIN_TRADE_HARD_RUB = 1000.0
_MIN_TRADE_HARD_CAP_PCT = 0.25
_SMALL_EQUITY_RUB = 5000.0
_SIGNAL_PAGE = 50
_SIGNAL_MAX_NAMES = 4
_SIGNAL_MAX_SMALL = 2
# TMOS: API 30052 Instrument forbidden. SIBN: нужен тест неквала, хозяин берёт в приложении.
# Котировки с ISS остаются, заявки на покупку через API — нет.
_API_BUY_BLOCKED = frozenset({"TMOS", "SIBN"})
_API_BUY_BLOCK_REASON = {
    "TMOS": "брокер запрещает заявку через API",
    "SIBN": "нужен тест в кабинете Т-Банка",
}
_API_FORBIDDEN_MARKERS = (
    "30052",
    "forbidden for trading by api",
    "sootvetstvuyushhij test",
    "projti sootvetstvuyushhij",
    "пройти соответствующий тест",
    "необходим тест",
)

_MARKET_WORDS = (
    "акци", "акцы", "портфел", "котиров", "бирж", "брокер",
    "мои бумаги", "ценные бумаги", "тихий счет", "тихий счёт",
)

_STATUS_ASK = (
    "что там", "как там", "как дела", "покажи", "скажи",
    "сколько", "какой ", "какая ", "какие ", "что с ", "что со ",
    "состояние", "сводка", "котиров",
)

_FOLLOWUP = (
    "подробнее", "подробней", "первая", "вторая", "другую",
    "ещё", "еще", "прибыл", "убыт", "процент",
)

_ENCYCLOPEDIA = (
    "что такое", "что значит", "кто такой", "кто такая",
    "расскажи про", "объясни что",
)

_PRICE_HINTS = (
    "сколько стоит", "какая цена", "цена на",
    "как там", "что там", "что со ", "что с ", "котиров",
)

_YIELD_HINTS = (
    "в плюсе", "в минусе", "прибыл", "убыт", "заработал",
    "доход", "сколько я", "результат",
)

_BUY_HINTS = ("купи", "докупи", "возьми")
_SELL_HINTS = ("продай",)
_AUTO_HINTS = (
    "поторгуй", "поторгуйся", "сыграй на бирже",
    "поработай счетом", "поработай счётом",
)
_ALLIN_HINTS = ("переложи", "вложи все", "вложи всё", "все в ", "всё в ")
_MAX_HINTS = ("все", "всё", "весь", "всю", "целиком", "максимум", "полностью")

_LOT_WORDS = {
    "один": 1, "одна": 1, "одно": 1, "раз": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}

# Голос Vosk; тикеры латиницей почти не распознаются.
_ALIASES = {
    "сбер": "SBER",
    "сбербанк": "SBER",
    "сбербанка": "SBER",
    "сбербанке": "SBER",
    "газпром": "GAZP",
    "газпрома": "GAZP",
    "газпроме": "GAZP",
    "лукойл": "LKOH",
    "лукайл": "LKOH",
    "лукойла": "LKOH",
    "яндекс": "YDEX",
    "яндекса": "YDEX",
    "норникель": "GMKN",
    "норильский": "GMKN",
    "роснефть": "ROSN",
    "роснефти": "ROSN",
    "втб": "VTBR",
    "вэтэбэ": "VTBR",
    "втэбэ": "VTBR",
    "банк втб": "VTBR",
    "втб банк": "VTBR",
    "тмос": "TMOS",
    "фонд": "TMOS",
    "крупнейшие": "TMOS",
    "крупнейших": "TMOS",
    "индекс мосбиржи": "TMOS",
    "магнит": "MGNT",
    "магнита": "MGNT",
    "озон": "OZON",
    "озона": "OZON",
    "аэрофлот": "AFLT",
    "аэрофлота": "AFLT",
    "мосбиржа": "MOEX",
    "биржа мос": "MOEX",
}

_SPOKEN = {
    "SBER": "Сбер",
    "GAZP": "Газпром",
    "LKOH": "Лукойл",
    "YDEX": "Яндекс",
    "YNDX": "Яндекс",
    "GMKN": "Норникель",
    "ROSN": "Роснефть",
    "VTBR": "ВТБ",
    "TMOS": "Крупнейшие компании",
    "MGNT": "Магнит",
    "OZON": "Озон",
    "AFLT": "Аэрофлот",
    "MOEX": "Мосбиржа",
    "T": "Т-Технологии",
    "TCSG": "Т-Технологии",
    "SIBN": "Газпром нефть",
    "NVTK": "Новатэк",
    "CNRU": "Китай",
}

def _http_error(response: requests.Response) -> RuntimeError:
    snippet = re.sub(r"t\.[A-Za-z0-9._-]{8,}", "[token]", (response.text or "").replace("\n", " "))
    return RuntimeError(f"http {response.status_code}: {snippet[:120]}")


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


def _sandbox() -> bool:
    return (os.getenv("TINKOFF_SANDBOX") or "").strip().lower() in {"1", "true", "yes", "on"}


def _tinkoff_verify() -> str | bool:
    """Сертификат Минцифры: без него Python не доверяет invest-public-api.tinkoff.ru."""
    extra = (os.getenv("TINKOFF_CA_BUNDLE") or "").strip()
    if extra and os.path.isfile(extra):
        return extra
    if os.path.isfile(_RU_CA):
        return _RU_CA
    return True


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


def _lots_phrase(n: int) -> str:
    return f"{n} {_plural(n, 'лот', 'лота', 'лотов')}"


def _min_trade_rub(equity: float) -> float:
    """Порог ребаланса: 2% капитала. Пол: 1000 ₽, только если счёт его выдерживает.

    Иначе мелкий фонд после продажи остаётся в кэше: рука 40% меньше тысячи,
    покупки отсекаются, а чужие бумаги режутся без порога.
    """
    equity = max(float(equity or 0.0), 1.0)
    pct = equity * _MIN_TRADE_PCT
    if _MIN_TRADE_HARD_RUB > equity * _MIN_TRADE_HARD_CAP_PCT:
        return max(pct, equity * _MIN_TRADE_SMALL_PCT)
    return max(pct, _MIN_TRADE_HARD_RUB)


def _signal_is_buy(direction: Any) -> bool:
    if direction in (1, "1"):
        return True
    return "BUY" in str(direction or "").upper()


def _is_api_buy_forbidden_text(text: str) -> bool:
    snippet = (text or "").lower()
    return any(marker in snippet for marker in _API_FORBIDDEN_MARKERS)


def _buy_block_reason(ticker: str) -> str:
    return _API_BUY_BLOCK_REASON.get(ticker.upper(), "через API заявку не принять")


def _signal_weight(raw: Any) -> float:
    try:
        weight = float(raw if raw is not None else 50.0)
    except (TypeError, ValueError):
        weight = 50.0
    return weight if weight > 0 else 50.0


def _quotation_to_float(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    units = int(value.get("units") or 0)
    nano = int(value.get("nano") or 0)
    return units + nano / 1_000_000_000.0


def _format_rub(amount: float, signed: bool = False) -> str:
    if not signed and 0 < abs(amount) < 30:
        rub = int(abs(amount))
        kop = int(round((abs(amount) - rub) * 100))
        if kop == 100:
            rub += 1
            kop = 0
        rub_word = _plural(rub, "рубль", "рубля", "рублей")
        if kop == 0:
            return f"{rub} {rub_word}"
        kop_word = _plural(kop, "копейка", "копейки", "копеек")
        return f"{rub} {rub_word} {kop} {kop_word}"
    rounded = int(round(amount))
    word = _plural(abs(rounded), "рубль", "рубля", "рублей")
    if signed:
        if rounded > 0:
            return f"плюс {rounded} {word}"
        if rounded < 0:
            return f"минус {abs(rounded)} {word}"
        return f"0 {word}"
    return f"{rounded} {word}"


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


def trading_temperature(base: float = 0.7) -> float:
    """Обычная рабочая температура Groq."""
    return base


def trading_reason_hint() -> str:
    """Системный хвост для Groq: спокойный уверенный тон без навязывания биржи."""
    return (
        "Биржу, брокерский счёт и котировки сам без прямого вопроса не поднимай. "
        "Говори спокойно, уверенно и по делу."
    )


def trading_clip_limit() -> tuple[int, int]:
    """(предложений, символов) для TTS."""
    return 3, 320


_desk_loop_started = False
_desk_init_lock = threading.Lock()
_TRADE_LOCK = threading.RLock()


def _auto_trade_enabled() -> bool:
    """Фоновые заявки: тумблер в настройках, иначе .env / песочница."""
    try:
        from skill_settings import is_auto_trade_enabled
        return is_auto_trade_enabled()
    except Exception:
        pass
    raw = (os.getenv("TINKOFF_AUTO_TRADE") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return _sandbox()


def _voice_trade_enabled() -> bool:
    """Заявки по команде («купи», «продай», «поторгуй»): тумблер, иначе .env."""
    try:
        from skill_settings import is_voice_trade_enabled
        return is_voice_trade_enabled()
    except Exception:
        pass
    raw = (os.getenv("TINKOFF_VOICE_TRADE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class StocksSkill(BaseSkill):
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
        self._manual_holds: set[str] = set()
        self._manual_tips_sent: set[str] = set()
        self._desk_stop = threading.Event()
        self._desk_enabled = threading.Event()
        self._desk_enabled.set()
        self._desk_thread: threading.Thread | None = None

    def _ensure_desk_loop(self) -> None:
        global _desk_loop_started
        with _desk_init_lock:
            if _desk_loop_started:
                return
            self._desk_thread = threading.Thread(
                target=self._desk_loop,
                name="stocks-desk",
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
        """Фоновый стол — после старта ассистента, не при импорте навыка."""
        self._ensure_desk_loop()

    def can_handle(self, context: RequestContext) -> bool:
        text = _norm(context.raw_text)
        if not text:
            return False
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
            if kind:
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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, service_method: str) -> str:
        root = _API_SANDBOX if _sandbox() else _API_PROD
        return f"{root}/tinkoff.public.invest.api.contract.v1.{service_method}"

    def _bust_broker_cache(self) -> None:
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if not key.startswith(("accounts", "pf:", "px:", "figi:", "find:", "max:"))
        }

    def _post(self, url: str, body: dict[str, Any], cache_key: str | None = None) -> dict[str, Any]:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit and time.time() - hit[0] < _CACHE_SEC:
                return hit[1]
        response = self._session.post(
            url,
            json=body,
            headers=self._headers(),
            timeout=6,
            verify=_tinkoff_verify(),
        )
        if response.status_code == 401:
            raise RuntimeError("token rejected")
        if response.status_code >= 400:
            snippet = (response.text or "")[:240].lower()
            if response.status_code in {403, 400} and any(
                marker in snippet
                for marker in ("permission", "прав", "readonly", "read only", "недостаточно прав")
            ):
                raise RuntimeError("trade token")
            if _is_api_buy_forbidden_text(snippet):
                raise RuntimeError("api forbidden")
            raise _http_error(response)
        data = response.json() if response.content else {}
        if cache_key:
            self._cache[cache_key] = (time.time(), data)
        return data

    def _get(self, url: str, params: dict[str, str], cache_key: str) -> dict[str, Any]:
        hit = self._cache.get(cache_key)
        if hit and time.time() - hit[0] < _CACHE_SEC:
            return hit[1]
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = self._session.get(url, params=params, timeout=6)
                if response.status_code >= 400:
                    raise _http_error(response)
                data = response.json() if response.content else {}
                self._cache[cache_key] = (time.time(), data)
                return data
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                time.sleep(0.4)
        if last_error:
            raise last_error
        raise RuntimeError("moex get failed")

    def _iss_rows(self, block: Any) -> list[dict[str, Any]]:
        if not isinstance(block, dict):
            return []
        cols = block.get("columns") or []
        return [dict(zip(cols, row)) for row in (block.get("data") or [])]

    def _load_book(self) -> list[dict[str, Any]]:
        # Локальная книжка бумаг без токена. Не коммитить (см. .gitignore).
        if not os.path.exists(_BOOK_PATH):
            return []
        try:
            with open(_BOOK_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            logger.warning("[Биржа] quiet_book.json: %s", exc)
            return []
        items = raw.get("positions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        book: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            book.append(
                {
                    "ticker": ticker,
                    "qty": float(item.get("qty") or 0),
                    "avg": float(item.get("avg") or 0),
                }
            )
        return book

    def _book_cash(self) -> float:
        if not os.path.exists(_BOOK_PATH):
            return 0.0
        try:
            with open(_BOOK_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return 0.0
        if isinstance(raw, dict):
            try:
                return float(raw.get("cash") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _broker_cash(self) -> float:
        if not self._token:
            return self._book_cash()
        account_id = self._pick_account_id()
        if not account_id:
            return 0.0
        try:
            data = self._post(
                self._url(_PORTFOLIO),
                {"accountId": account_id, "currency": "RUB"},
                cache_key=f"pf:{account_id}",
            )
            cash_obj = data.get("totalAmountCurrencies") or {}
            cash = _quotation_to_float(cash_obj)
            # С токеном нулевой кэш брокера — это ноль, а не устаревшая quiet_book.
            return max(cash, 0.0)
        except Exception as exc:
            logger.warning("[Биржа] Не удалось получить кэш брокера: %s", exc)
            return 0.0

    def _watch_tickers(self) -> list[str]:
        ordered: list[str] = []
        for item in self._load_book():
            ticker = item["ticker"]
            if ticker not in ordered:
                ordered.append(ticker)
        for ticker in self._watchlist:
            if ticker not in ordered:
                ordered.append(ticker)
        return ordered

    def _pick_marketdata_row(self, rows: list[dict[str, Any]]) -> tuple[float, float] | None:
        preferred = []
        others = []
        for row in rows:
            board = str(row.get("BOARDID") or "")
            last = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE")
            if last in (None, "", 0, 0.0):
                continue
            bucket = preferred if board in _MOEX_BOARDS else others
            bucket.append((float(last), float(row.get("LASTTOPREVPRICE") or 0)))
        chosen = preferred or others
        return chosen[0] if chosen else None

    def _moex_quote(self, ticker: str) -> tuple[str, float, float]:
        ticker = ticker.upper()
        last_error: Exception | None = None
        urls = [f"{_MOEX}/boards/{board}/securities/{ticker}.json" for board in _MOEX_BOARDS]
        urls.append(f"{_MOEX}/securities/{ticker}.json")
        for url in urls:
            try:
                data = self._get(
                    url,
                    {"iss.meta": "off", "iss.only": "marketdata"},
                    f"moex:{url}",
                )
            except Exception as exc:
                last_error = exc
                logger.debug("[Биржа] MOEX %s: %s", url, exc)
                continue
            picked = self._pick_marketdata_row(self._iss_rows(data.get("marketdata")))
            if not picked:
                continue
            last, pct = picked
            spoken = self._spoken_name(ticker)
            self._remember_name(ticker, spoken)
            return spoken, last, pct
        if last_error:
            raise last_error
        raise RuntimeError(f"no moex quote {ticker}")

    def _quote(self, ticker: str) -> tuple[str, float, float]:
        if self._token:
            try:
                name, price = self._tinkoff_last(ticker)
                return name, price, 0.0
            except Exception as exc:
                logger.warning("[Биржа] котировка брокера недоступна: %s", exc)
        return self._moex_quote(ticker)

    def _ticker_from_text(self, text: str) -> str | None:
        aliases = dict(_ALIASES)
        aliases.update(self._alias_extra)
        for name in sorted(aliases, key=len, reverse=True):
            if re.search(rf"(?<![а-яa-z]){re.escape(name)}(?![а-яa-z])", text):
                return aliases[name]
        for ticker in list(self._watchlist) + list(self._last_tickers):
            if ticker and ticker.lower() in text:
                return ticker
        return None

    def _pick_account_id(self) -> str | None:
        if self._account_id:
            return self._account_id
        data = self._post(self._url(_USERS), {}, cache_key="accounts")
        accounts = data.get("accounts") or []
        open_accounts = [
            acc for acc in accounts
            if acc.get("status") in (None, "ACCOUNT_STATUS_OPEN", "ACCOUNT_STATUS_UNSPECIFIED")
        ]
        if not open_accounts:
            open_accounts = accounts
        preferred = (
            "ACCOUNT_TYPE_TINKOFF",
            "ACCOUNT_TYPE_TINKOFF_IIS",
            "ACCOUNT_TYPE_INVEST_BOX",
        )
        for kind in preferred:
            for acc in open_accounts:
                if acc.get("type") == kind and acc.get("id"):
                    return str(acc["id"])
        for acc in open_accounts:
            if acc.get("id"):
                return str(acc["id"])
        return None

    def _instrument(self, figi: str | None, ticker: str | None, uid: str | None = None) -> dict[str, Any]:
        if uid:
            try:
                data = self._post(
                    self._url(_BY_ID),
                    {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid},
                    cache_key=f"uid:{uid}",
                )
                inst = data.get("instrument") or {}
                if inst:
                    return inst
            except Exception as exc:
                logger.debug("[Биржа] инструмент uid %s: %s", uid, exc)
        if figi:
            data = self._post(
                self._url(_BY_ID),
                {"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi},
                cache_key=f"figi:{figi}",
            )
            inst = data.get("instrument") or {}
            if inst:
                return inst
        if ticker:
            ticker_up = ticker.upper()
            for class_code in _MOEX_BOARDS:
                try:
                    data = self._post(
                        self._url(_BY_ID),
                        {
                            "idType": "INSTRUMENT_ID_TYPE_TICKER",
                            "classCode": class_code,
                            "id": ticker_up,
                        },
                        cache_key=f"by:{class_code}:{ticker_up}",
                    )
                except Exception:
                    continue
                inst = data.get("instrument") or {}
                if inst.get("figi") or inst.get("uid") or inst.get("instrumentId"):
                    return inst
            kinds = (
                None,
                "INSTRUMENT_TYPE_SHARE",
                "INSTRUMENT_TYPE_ETF",
            )
            for kind in kinds:
                body: dict[str, Any] = {
                    "query": ticker,
                    "apiTradeAvailableFlag": True,
                }
                if kind:
                    body["instrumentKind"] = kind
                cache = f"find:{ticker}:{kind or 'any'}"
                data = self._post(self._url(_FIND), body, cache_key=cache)
                found = None
                for inst in data.get("instruments") or []:
                    if str(inst.get("ticker") or "").upper() != ticker_up:
                        continue
                    class_code = str(inst.get("classCode") or "")
                    if class_code in ("TQBR", "TQTF", "TQPI", ""):
                        return inst
                    found = inst
                if found:
                    return found
        return {}

    def _remember_name(self, ticker: str, spoken: str) -> None:
        ticker = ticker.upper()
        spoken_key = _norm(spoken)
        if spoken_key:
            self._alias_extra[spoken_key] = ticker
        if ticker not in self._last_tickers:
            self._last_tickers.append(ticker)
            self._last_tickers = self._last_tickers[-8:]

    def _spoken_name(self, ticker: str, instrument: dict[str, Any] | None = None) -> str:
        if ticker in _SPOKEN:
            return _SPOKEN[ticker]
        name = str((instrument or {}).get("name") or "").strip()
        if name:
            return name.split(",")[0].strip()
        return ticker

    def _positions(self) -> tuple[list[dict[str, Any]], float, float]:
        account_id = self._pick_account_id()
        if not account_id:
            raise RuntimeError("no account")
        data = self._post(
            self._url(_PORTFOLIO),
            {"accountId": account_id, "currency": "RUB"},
            cache_key=f"pf:{account_id}",
        )
        positions: list[dict[str, Any]] = []
        for raw in data.get("positions") or []:
            kind = str(raw.get("instrumentType") or "").lower()
            if kind not in {"share", "etf", "instrument_type_share", "instrument_type_etf"}:
                continue
            figi = raw.get("figi")
            ticker = str(raw.get("ticker") or "").upper()
            instrument = self._instrument(figi, ticker or None)
            ticker = ticker or str(instrument.get("ticker") or "").upper()
            if not ticker:
                continue
            qty = _quotation_to_float(raw.get("quantity"))
            price = _quotation_to_float(raw.get("currentPrice"))
            avg = _quotation_to_float(raw.get("averagePositionPrice"))
            expected = _quotation_to_float(raw.get("expectedYield"))
            daily = _quotation_to_float(raw.get("dailyYield"))
            spoken = self._spoken_name(ticker, instrument)
            self._remember_name(ticker, spoken)
            positions.append(
                {
                    "ticker": ticker,
                    "name": spoken,
                    "qty": qty,
                    "price": price,
                    "avg": avg,
                    "yield": expected,
                    "daily": daily,
                }
            )
        day_total = _quotation_to_float(data.get("dailyYield"))
        if not day_total:
            day_total = sum(item["daily"] for item in positions)
        total_yield = _quotation_to_float(data.get("expectedYield"))
        return positions, day_total, total_yield

    def _tinkoff_last(self, ticker: str) -> tuple[str, float]:
        instrument = self._instrument(None, ticker)
        figi = instrument.get("figi")
        if not figi:
            raise RuntimeError(f"unknown ticker {ticker}")
        data = self._post(self._url(_PRICES), {"figi": [figi]}, cache_key=f"px:{figi}")
        prices = data.get("lastPrices") or []
        price = _quotation_to_float(prices[0].get("price")) if prices else 0.0
        spoken = self._spoken_name(ticker, instrument)
        self._remember_name(ticker, spoken)
        return spoken, price

    def _last_price(self, ticker: str) -> tuple[str, float]:
        name, price, _pct = self._quote(ticker)
        return name, price

    def _line(self, item: dict[str, Any]) -> str:
        price = item["price"]
        avg = item["avg"]
        pct = 0.0
        if avg:
            pct = (price - avg) / avg * 100.0
        elif item["yield"] and price and item["qty"]:
            cost = price * item["qty"] - item["yield"]
            if cost:
                pct = item["yield"] / cost * 100.0
        return f"{item['name']} {_format_rub(price)}, {_format_pct(pct)}"

    def _speak_one(self, ticker: str) -> str:
        if self._token:
            positions, day_total, _total_yield = self._safe_positions()
            for item in positions:
                if item["ticker"] == ticker:
                    extra = ""
                    if item["daily"]:
                        extra = f" За день {_format_rub(item['daily'], signed=True)}."
                    return self._line(item) + "." + extra
        book = {item["ticker"]: item for item in self._load_book()}
        name, price, day_pct = self._quote(ticker)
        pos = book.get(ticker) or {}
        avg = float(pos.get("avg") or 0)
        qty = float(pos.get("qty") or 0)
        if avg:
            pnl = (price - avg) * qty if qty else 0.0
            speech = f"{name} {_format_rub(price)}, {_format_pct((price - avg) / avg * 100.0)}."
            if qty:
                speech += f" С покупки {_format_rub(pnl, signed=True)}."
        else:
            speech = f"{name} {_format_rub(price)}, {_format_pct(day_pct)} за день."
        if qty and day_pct:
            daily = qty * price * day_pct / 100.0
            speech += f" За день {_format_rub(daily, signed=True)}."
        return speech

    def _speak_watch(self, emphasize_yield: bool) -> str:
        tickers = self._watch_tickers()
        if not tickers:
            return "Ключа от счёта ещё нет. Назови бумагу — сверю по бирже."
        book = {item["ticker"]: item for item in self._load_book()}
        parts: list[str] = []
        day_total = 0.0
        total_pnl = 0.0
        have_day = False
        have_pnl = False
        for ticker in tickers[:4]:
            name, price, day_pct = self._quote(ticker)
            pos = book.get(ticker) or {}
            avg = float(pos.get("avg") or 0)
            qty = float(pos.get("qty") or 0)
            if avg:
                parts.append(f"{name} {_format_rub(price)}, {_format_pct((price - avg) / avg * 100.0)}")
                if qty:
                    total_pnl += (price - avg) * qty
                    have_pnl = True
            else:
                parts.append(f"{name} {_format_rub(price)}, {_format_pct(day_pct)} за день")
            if qty:
                day_total += qty * price * (day_pct / 100.0)
                have_day = True
        speech = ". ".join(parts) + "."
        if have_pnl:
            speech += f" С покупки {_format_rub(total_pnl, signed=True)}."
        elif emphasize_yield and have_day:
            speech += f" За день {_format_rub(day_total, signed=True)}."
        cash = self._book_cash()
        if cash:
            speech += f" Кэшем {_format_rub(cash)}."
        return speech

    def _speak_portfolio(self, emphasize_yield: bool) -> str:
        positions, day_total, total_yield = self._positions()
        if not positions:
            if self._watchlist:
                parts = []
                for ticker in self._watchlist[:4]:
                    name, price = self._last_price(ticker)
                    parts.append(f"{name} {_format_rub(price)}")
                return "Наблюдаю: " + "; ".join(parts) + "."
            return "На тихом счёте сейчас нет акций."

        lines = [self._line(item) for item in positions[:4]]
        speech = ". ".join(lines) + "."
        if emphasize_yield or len(positions) > 1:
            if day_total:
                speech += f" За день {_format_rub(day_total, signed=True)}."
            elif total_yield:
                speech += f" С покупки {_format_rub(total_yield, signed=True)}."
        return speech

    def _safe_positions(self) -> tuple[list[dict[str, Any]], float, float]:
        try:
            return self._positions()
        except Exception as exc:
            logger.warning("[Биржа] Портфель недоступен: %s", exc)
            return [], 0.0, 0.0

    def _market_open(self) -> bool:
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        main = 10 * 60 <= minutes <= 18 * 60 + 50
        evening = 19 * 60 + 5 <= minutes <= 23 * 60 + 50
        return main or evening

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
        verb = "купил" if direction == "ORDER_DIRECTION_BUY" else "продал"
        return f"{verb.capitalize()} {_lots_phrase(lots)}: {spoken}."

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
        with _TRADE_LOCK:
            return self._execute_trade_locked(text, kind, ticker)

    def _execute_trade_locked(self, text: str, kind: str, ticker: str | None) -> str:
        if not _voice_trade_enabled():
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

    def _held_map(self) -> dict[str, float]:
        held: dict[str, float] = {}
        if self._token:
            positions, _day, _total = self._safe_positions()
            for item in positions:
                held[item["ticker"]] = float(item.get("qty") or 0)
            return held
        for item in self._load_book():
            held[item["ticker"]] = float(item.get("qty") or 0)
        return held

    def _equity_estimate(self, tape: list[dict[str, Any]] | None = None) -> float:
        prices = {row["ticker"]: row["price"] for row in (tape or [])}
        held = self._held_map()
        total = self._broker_cash()
        for ticker, qty in held.items():
            price = prices.get(ticker)
            if price is None:
                try:
                    _name, price, _pct = self._moex_quote(ticker)
                except Exception:
                    price = 0.0
            total += qty * float(price or 0)
        return max(total, 1.0)

    def _tqbr_tape(self) -> list[dict[str, Any]]:
        data = self._get(
            f"{_MOEX}/boards/TQBR/securities.json",
            {"iss.meta": "off", "iss.only": "securities,marketdata"},
            "moex:tqbr:tape",
        )
        sec_rows = {row.get("SECID"): row for row in self._iss_rows(data.get("securities"))}
        tape: list[dict[str, Any]] = []
        for row in self._iss_rows(data.get("marketdata")):
            ticker = str(row.get("SECID") or "").upper()
            last = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE")
            if not ticker or last in (None, "", 0, 0.0):
                continue
            sec = sec_rows.get(ticker) or {}
            lot = sec.get("LOTSIZE") or 1
            try:
                lot = max(int(lot), 1)
            except (TypeError, ValueError):
                lot = 1
            name = str(sec.get("SHORTNAME") or sec.get("SECNAME") or ticker)
            pct = float(row.get("LASTTOPREVPRICE") or 0)
            value = float(row.get("VALTODAY") or row.get("VALUE") or 0)
            tape.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "price": float(last),
                    "pct": pct,
                    "lot": lot,
                    "value": value,
                }
            )
            self._remember_name(ticker, name.split()[0] if name else ticker)
        return tape

    def _desk_candidates(self) -> list[dict[str, Any]]:
        try:
            tape = self._tqbr_tape()
        except Exception as exc:
            logger.warning("[Биржа] лента TQBR: %s", exc)
            tape = []
        held = self._held_map()
        equity = self._equity_estimate(tape)
        from_tape = {row["ticker"]: row for row in tape}
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            ticker = row["ticker"]
            if ticker in seen:
                return
            seen.add(ticker)
            chosen.append(row)

        for ticker, qty in held.items():
            row = from_tape.get(ticker)
            if row is None:
                try:
                    name, price, pct = self._moex_quote(ticker)
                    row = {"ticker": ticker, "name": name, "price": price, "pct": pct, "lot": 1, "value": 0}
                except Exception:
                    continue
            row = dict(row)
            row["held"] = qty
            add(row)

        # Мелкий фонд: не гоняемся за всей лентой TQBR — только то, что уже есть
        # и watchlist, если лот реально влезает в капитал.
        if equity < _SMALL_EQUITY_RUB:
            for ticker in self._watchlist:
                ticker = ticker.upper()
                if ticker in seen:
                    continue
                row = from_tape.get(ticker)
                if row is None:
                    try:
                        name, price, pct = self._moex_quote(ticker)
                        row = {
                            "ticker": ticker,
                            "name": name,
                            "price": price,
                            "pct": pct,
                            "lot": 1,
                            "value": 0,
                        }
                    except Exception:
                        continue
                lot = max(int(row.get("lot") or 1), 1)
                if row["price"] * lot > equity * 0.98:
                    continue
                add(dict(row, held=held.get(ticker, 0)))
            return chosen

        affordable = [
            row
            for row in tape
            if row["ticker"] not in seen
            and row["price"] * row["lot"] <= equity * 0.98
            and row["value"] >= 500_000
        ]
        affordable.sort(key=lambda row: abs(row["pct"]), reverse=True)
        for row in affordable:
            if len(chosen) >= 12:
                break
            add(dict(row, held=0))
        return chosen

    def _lot_size(self, ticker: str) -> int:
        ticker = ticker.upper()
        if self._token:
            try:
                inst = self._instrument(None, ticker)
                lot = int(inst.get("lot") or 0)
                if lot > 0:
                    return lot
            except Exception:
                pass
        try:
            for row in self._tqbr_tape():
                if row.get("ticker") == ticker:
                    return int(row.get("lot") or 1)
        except Exception:
            pass
        return 1

    def _is_buy_blocked(self, ticker: str) -> bool:
        ticker = ticker.upper()
        return ticker in _API_BUY_BLOCKED or ticker in self._buy_blocked

    def _filter_buy_alloc(
        self,
        alloc: dict[str, float],
        candidates: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Убирает бумаги, которые API не покупает, и нормализует доли на 100%."""
        cleaned: dict[str, float] = {}
        for ticker, pct in (alloc or {}).items():
            ticker_u = str(ticker).upper().strip()
            if not ticker_u or self._is_buy_blocked(ticker_u):
                continue
            try:
                val = float(pct)
            except (TypeError, ValueError):
                continue
            if val > 0:
                cleaned[ticker_u] = val
        if not cleaned:
            return self._fallback_allocation(candidates)
        total = sum(cleaned.values())
        if total <= 0:
            return self._fallback_allocation(candidates)
        return {key: round((val / total) * 100.0, 1) for key, val in cleaned.items()}

    def _fallback_allocation(self, candidates: list[dict[str, Any]]) -> dict[str, float]:
        """Фолбек: первая бумага из списка, которую API позволяет купить."""
        for row in candidates:
            ticker = str(row.get("ticker") or "").upper()
            if ticker and not self._is_buy_blocked(ticker):
                return {ticker: 100.0}
        return {}

    def _fetch_buy_signals(self) -> list[dict[str, Any]]:
        if not self._token:
            return []
        try:
            data = self._post(
                self._url(_SIGNALS),
                {
                    "direction": "SIGNAL_DIRECTION_BUY",
                    "active": "SIGNAL_STATE_ACTIVE",
                    "paging": {"limit": _SIGNAL_PAGE, "pageNumber": 0},
                },
                cache_key="signals:buy:active",
            )
        except Exception as exc:
            logger.warning("[Биржа] сигналы Т-Инвест: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in data.get("signals") or []:
            if isinstance(raw, dict) and _signal_is_buy(raw.get("direction")):
                out.append(raw)
        return out

    def _signal_lot_cost(
        self,
        ticker: str,
        inst: dict[str, Any],
        lot_cost: dict[str, float],
    ) -> float:
        if ticker in lot_cost:
            return lot_cost[ticker]
        lot = max(int(inst.get("lot") or 1), 1)
        try:
            _name, price, _pct = self._quote(ticker)
        except Exception:
            return 0.0
        cost = float(price or 0) * lot
        if cost > 0:
            lot_cost[ticker] = cost
        return cost

    def _recommend_manual_buys(self, tickers: list[str]) -> None:
        """Раз: сигнал есть, через API не купить — написать хозяину в Telegram."""
        fresh: list[str] = []
        for ticker in tickers:
            ticker_u = str(ticker or "").upper().strip()
            if not ticker_u or ticker_u in self._manual_tips_sent:
                continue
            self._manual_tips_sent.add(ticker_u)
            self._manual_holds.add(ticker_u)
            fresh.append(ticker_u)
        if not fresh:
            return
        lines = [
            f"{self._spoken_name(ticker)} ({ticker}) — {_buy_block_reason(ticker)}"
            for ticker in fresh
        ]
        if len(lines) == 1:
            text = (
                f"Сигнал Т-Инвест: {lines[0]}. "
                "Сам не куплю. Если согласен — возьми в приложении."
            )
        else:
            text = (
                "Сигналы, которые сам не куплю:\n"
                + "\n".join(f"• {line}" for line in lines)
                + "\nЕсли согласен — возьми в приложении."
            )
        logger.info("[Биржа] рекомендация хозяину: %s", text)
        if telegram_configured():
            send_telegram_notification(text)

    def _desk_choose(self, candidates: list[dict[str, Any]], equity: float | None = None) -> dict[str, float]:
        if equity is None:
            equity = self._equity_estimate(candidates)
        small = equity < _SMALL_EQUITY_RUB
        lot_cost = {
            str(row["ticker"]).upper(): float(row.get("price") or 0) * max(int(row.get("lot") or 1), 1)
            for row in candidates
            if row.get("ticker")
        }
        scores: dict[str, float] = {}
        blocked_scores: dict[str, float] = {}
        for sig in self._fetch_buy_signals():
            uid = str(sig.get("instrumentUid") or sig.get("instrument_uid") or "").strip()
            if not uid:
                continue
            inst = self._instrument(None, None, uid=uid)
            ticker = str(inst.get("ticker") or "").upper()
            if not ticker:
                continue
            class_code = str(inst.get("classCode") or "")
            if class_code and class_code not in _MOEX_BOARDS:
                continue
            kind = str(inst.get("instrumentKind") or inst.get("instrumentType") or "").upper()
            if kind and "SHARE" not in kind and "ETF" not in kind:
                continue
            cost = self._signal_lot_cost(ticker, inst, lot_cost)
            if cost <= 0 or cost > equity * 0.98:
                continue
            weight = _signal_weight(sig.get("probability"))
            self._remember_name(ticker, self._spoken_name(ticker, inst))
            blocked = self._is_buy_blocked(ticker) or inst.get("apiTradeAvailableFlag") is False
            if blocked:
                blocked_scores[ticker] = blocked_scores.get(ticker, 0.0) + weight
                continue
            scores[ticker] = scores.get(ticker, 0.0) + weight

        limit = _SIGNAL_MAX_SMALL if small else _SIGNAL_MAX_NAMES
        ranked_all = sorted(
            list(scores.items()) + list(blocked_scores.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        tips = [ticker for ticker, _weight in ranked_all[:limit] if ticker in blocked_scores]
        if tips:
            self._recommend_manual_buys(tips)

        if not scores:
            logger.info("[Биржа] активных сигналов Т-Инвест нет, фоллбек")
            return self._fallback_allocation(candidates)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        picked = ranked[:limit]
        total = sum(value for _ticker, value in picked)
        if total <= 0:
            return self._fallback_allocation(candidates)
        alloc = {ticker: round((value / total) * 100.0, 1) for ticker, value in picked}
        logger.info("[Биржа] сигналы Т-Инвест: %s", alloc)
        return alloc

    def _rebalance_portfolio(self, target_alloc: dict[str, float], silent: bool = False) -> str:
        parts: list[str] = []
        acted = False
        target_alloc = self._filter_buy_alloc(
            target_alloc,
            [{"ticker": str(ticker)} for ticker in (target_alloc or {})],
        )
        if not target_alloc:
            result = "Нечего покупать: брокер не даёт эти бумаги через API."
            if silent:
                logger.info("[Биржа] авто-ребалансировка: %s", result)
            return result

        positions_list, _day, _total = self._safe_positions()
        positions = {p["ticker"]: p for p in positions_list}
        cash = self._broker_cash()

        # Общая стоимость портфеля (Equity = Cash + Стоимость всех позиций)
        total_pos_value = sum(p["price"] * p["qty"] for p in positions.values())
        equity = max(cash + total_pos_value, 1.0)
        min_trade_rub = _min_trade_rub(equity)

        # Собираем актуальные цены и размер лотов для всех задействованных бумаг
        relevant_tickers = set(positions.keys()) | set(target_alloc.keys())
        prices: dict[str, float] = {}
        lotsizes: dict[str, int] = {}

        for ticker in relevant_tickers:
            if ticker in positions and positions[ticker].get("price", 0) > 0:
                prices[ticker] = positions[ticker]["price"]
            else:
                try:
                    _, p, _ = self._quote(ticker)
                    prices[ticker] = p
                except Exception:
                    prices[ticker] = 0.0
            lotsizes[ticker] = self._lot_size(ticker)

        # Целевая и текущая стоимость позиций
        target_values = {
            ticker: equity * (target_alloc.get(ticker, 0.0) / 100.0)
            for ticker in relevant_tickers
        }
        current_values = {
            ticker: positions.get(ticker, {}).get("qty", 0.0) * prices.get(ticker, 0.0)
            for ticker in relevant_tickers
        }

        # 1. Цикл ПРОДАЖИ: сокращаем или полностью закрываем позиции, чья доля выше целевой
        sell_candidates: list[tuple[str, float]] = []
        for ticker in relevant_tickers:
            diff = current_values[ticker] - target_values[ticker]
            # Полная ликвидация исключенных — тоже с порогом, чтобы мелкий фонд
            # не продавал всё в кэш, который потом не набирает лоты цели.
            if diff > 0 and diff >= min_trade_rub:
                sell_candidates.append((ticker, diff))

        sell_candidates.sort(key=lambda item: item[1], reverse=True)

        for ticker, excess_rub in sell_candidates:
            _buy_max, sell_max = self._max_lots(ticker)
            if sell_max <= 0:
                continue

            price = prices.get(ticker, 0.0)
            lot_size = lotsizes.get(ticker, 1)
            lot_cost = price * lot_size

            # Если бумаги нет в целевом портфеле — продаем весь доступный объем.
            # Рекомендованные хозяину бумаги не трогаем: он мог купить их сам.
            if target_alloc.get(ticker, 0.0) <= 0:
                if ticker in self._manual_holds:
                    continue
                lots_to_sell = sell_max
            else:
                lots_to_sell = int(excess_rub / lot_cost) if lot_cost > 0 else 0
                lots_to_sell = min(lots_to_sell, sell_max)

            if lots_to_sell > 0:
                try:
                    res = self._place_order(ticker, "ORDER_DIRECTION_SELL", lots_to_sell)
                    parts.append(res)
                    acted = True
                    time.sleep(0.6)
                except Exception as exc:
                    logger.warning("[Биржа] Ошибка при продаже %s: %s", ticker, exc)

        # После продаж пересчитываем позиции и кэш — иначе покупки идут по старым долям.
        if acted:
            time.sleep(1.0)
            self._bust_broker_cache()
            positions_list, _day, _total = self._safe_positions()
            positions = {item["ticker"]: item for item in positions_list}
            current_values = {
                ticker: positions.get(ticker, {}).get("qty", 0.0) * prices.get(ticker, 0.0)
                for ticker in relevant_tickers
            }

        # 2. Цикл ПОКУПКИ: принцип «Сначала считаем — потом покупаем»
        available_cash: float | None = self._broker_cash()
        if self._token and (available_cash or 0) < min_trade_rub:
            # После продаж кэш в GetPortfolio иногда ещё ноль; лимит — GetMaxLots.
            available_cash = None
        buy_candidates: list[tuple[str, float]] = []
        for ticker, target_pct in target_alloc.items():
            if self._is_buy_blocked(ticker):
                continue
            diff = target_values[ticker] - current_values.get(ticker, 0.0)
            if diff >= min_trade_rub:
                buy_candidates.append((ticker, diff))

        buy_candidates.sort(key=lambda item: item[1], reverse=True)

        # Формируем предварительный план покупок с учетом доступного кэша
        planned_buys: list[tuple[str, int]] = []
        for ticker, need_rub in buy_candidates:
            price = prices.get(ticker, 0.0)
            lot_size = lotsizes.get(ticker, 1)
            lot_cost = price * lot_size
            if lot_cost <= 0:
                continue

            desired_lots = int(need_rub / lot_cost)
            if desired_lots <= 0:
                continue

            if available_cash is not None and desired_lots * lot_cost > available_cash:
                desired_lots = int(available_cash / lot_cost)

            if desired_lots > 0:
                if available_cash is not None:
                    available_cash -= desired_lots * lot_cost
                planned_buys.append((ticker, desired_lots))

        # Выполняем ордера по составленному плану покупок
        for ticker, planned_lots in planned_buys:
            buy_max, _sell_max = self._max_lots(ticker)
            lots_to_buy = min(planned_lots, buy_max)
            if lots_to_buy <= 0:
                continue

            try:
                res = self._place_order(ticker, "ORDER_DIRECTION_BUY", lots_to_buy)
                parts.append(res)
                acted = True
                time.sleep(0.6)
                self._bust_broker_cache()
            except Exception as exc:
                logger.warning("[Биржа] Ошибка при покупке %s: %s", ticker, exc)

        if not acted or not parts:
            leftover = any(
                (not self._is_buy_blocked(ticker))
                and (target_alloc.get(ticker, 0.0) > 0)
                and (target_values.get(ticker, 0.0) - current_values.get(ticker, 0.0) >= min_trade_rub)
                for ticker in target_alloc
            )
            if leftover:
                result = "Цель есть, но свободных лотов пока не набралось."
            else:
                result = "Портфель уже сбалансирован в целевых долях."
        else:
            result = "Ребалансировал портфель: " + " ".join(parts)

        if silent:
            logger.info("[Биржа] авто-ребалансировка: %s", result)
            return result
        return result

    def _trade_auto(self, silent: bool = False) -> str:
        """Сам формирует сбалансированный портфель и ребалансирует фонд."""
        with _TRADE_LOCK:
            return self._trade_auto_locked(silent)

    def _trade_auto_locked(self, silent: bool = False) -> str:
        candidates = self._desk_candidates()
        if not candidates:
            return "Нечего решать: лента пуста."
        alloc = self._filter_buy_alloc(
            self._desk_choose(candidates, equity=self._equity_estimate(candidates)),
            candidates,
        )
        if not alloc:
            return "Нечего покупать: брокер не даёт эти бумаги через API."

        alloc_parts = []
        for ticker, pct in alloc.items():
            name = self._spoken_name(ticker)
            alloc_parts.append(f"{name} {int(round(pct))}%")
        alloc_desc = ", ".join(alloc_parts)
        logger.info("[Биржа] целевой портфель: %s", alloc_desc)

        if not self._token:
            return f"Целевой портфель: {alloc_desc}. Торгового ключа нет, пока держу на бумаге."

        if not self._market_open():
            raise RuntimeError("market closed")

        return self._rebalance_portfolio(alloc, silent=silent)

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
        # Сам ходит, пока биржа открыта, навык включён и разрешена автоторговля.
        # После старта: 90 с, затем период 45 мин — без сделки сразу при запуске.
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
                self._token = (
                    os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN") or ""
                ).strip()
                if not self._market_open():
                    continue
                self._trade_auto(silent=True)
            except Exception as exc:
                logger.warning("[Биржа] фоновый цикл торговли: %s", exc)

