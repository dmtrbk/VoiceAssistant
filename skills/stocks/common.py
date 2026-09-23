# skills/stocks/common.py
# Общие константы, пути и мелкие помощники биржи Т-Инвест.

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

from skills.text_utils import plural as _plural
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
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BOOK_PATH = os.path.join(_PROJECT_DIR, "quiet_book.json")
_HOLD_PATH = os.path.join(_PROJECT_DIR, "jarvis_holds.json")
_BOUGHT_PATH = os.path.join(_PROJECT_DIR, "jarvis_bought.json")
_TRADE_PATH = os.path.join(_PROJECT_DIR, "jarvis_trades.json")
_ALLOC_PATH = os.path.join(_PROJECT_DIR, "jarvis_stocks_alloc.json")
_MOEX_HISTORY = "https://iss.moex.com/iss/history/engines/stock/markets/shares"
_RU_CA = os.path.join(_PROJECT_DIR, "certs", "russian_trusted_root_ca.pem")
_DESK_PERIOD_SEC = 3 * 60 * 60  # полный скор сигналов + ребаланс
_WATCH_PERIOD_SEC = 15 * 60  # дозор: к сохранённой цели, без нового скора
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
_ADVICE_HINTS = (
    "посоветуй",
    "рекомендаци",
    "разбери сигнал",
    "что по сигналам",
    "пришли совет",
    "советник",
)
_JOURNAL_HINTS = ("дневник", "журнал сделок")
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
    return {str(item).upper().strip() for item in raw if str(item).strip()}


def _write_ticker_set(path: str, tickers: set[str]) -> None:
    payload = {"tickers": sorted(tickers)}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Биржа] не записал %s: %s", os.path.basename(path), exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def read_alloc_state(path: str | None = None) -> dict[str, Any] | None:
    """Последняя цель стола. None — файла нет / битый."""
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
        ticker = str(key or "").upper().strip()
        if not ticker:
            continue
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            alloc[ticker] = pct
    return {
        "alloc": alloc,
        "why": str(raw.get("why") or ""),
        "ts": float(raw.get("ts") or 0),
    }


def write_alloc_state(
    alloc: dict[str, float],
    *,
    why: str = "",
    path: str | None = None,
) -> None:
    path = path or _ALLOC_PATH
    clean = {
        str(k).upper().strip(): float(v)
        for k, v in (alloc or {}).items()
        if str(k).strip() and float(v) > 0
    }
    payload = {"alloc": clean, "why": str(why or ""), "ts": time.time()}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Биржа] не записал цель стола: %s", exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


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
    """Системный хвост для Groq: характер фонда, без навязанных котировок."""
    return (
        "Если спрашивают, откуда деньги на старт — ты дал сто долларов, не «Дмитрий вложил» в третьем лице и не сбережения. "
        "Крипта не в Т-Инвесте. Нет факта — не выдумывай, лучше «не знаю». "
        "Котировки и заявки сам не зачитывай. Биржу в свет и охрану не тащи. "
        "Иногда можно коротко заговорить о фонде, Linux или железе — полфразы, сухой юмор по делу, без «почему». "
        "Не проси купить железо. Говори спокойно, уверенно и по делу."
    )


def trading_clip_limit() -> tuple[int, int]:
    """(предложений, символов) для TTS."""
    return 3, 320


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

_desk_loop_started = False
_desk_init_lock = threading.Lock()
_TRADE_LOCK = threading.RLock()
