#!/usr/bin/env python3
# conky_markets.py
# Две строки для Conky: дневной +/- биржи (Т-Инвест) и крипты (Bybit).
# Обе: день / заработано (ASCII `/`), только цифры. Без ∑ — Candara.
# Вторая цифра крипты — реальные деньги: закрытые сделки + проценты Earn, без бумажной переоценки.
# Цвета из ~/.conky: плюс c0c0c0 (system), минус 888888 (comands default).
# Кэш 90 мин, сразу после сделки (дневник новее кэша). Стол заявок не запускает.

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

COLOR_PLUS = "c0c0c0"
COLOR_MINUS = "888888"
CACHE_PATH = os.path.join(_PROJECT_DIR, ".conky_markets.cache")
PNL_CACHE_PATH = os.path.join(_PROJECT_DIR, ".conky_markets.pnl.json")
CACHE_MAX_AGE = 90 * 60
JOURNAL_PATHS = (
    os.path.join(_PROJECT_DIR, "jarvis_crypto_trades.json"),
    os.path.join(_PROJECT_DIR, "jarvis_trades.json"),
)

# «как дела» и близкие — не сводка портфеля, а настроение дня по тем же +/- что Conky.
_HOW_ARE_YOU = (
    "как дела",
    "как ты",
    "как жизнь",
    "как сам",
    "что как",
    "как оно",
    "как настроение",
)
_TODAY_CLARIFY = (
    "за сегодня",
    "это за сегодня",
    "за сутки",
    "дневной",
    "дневные",
    "за день",
)
_ALL_TIME = (
    "за всё время",
    "за все время",
    "с покупки",
    "за все время?",
    "а за всё",
    "а за все",
)
_IRON_PACE = (
    "такими темпами",
    "переедешь",
    "переедем",
    "новое железо",
    "на новое железо",
    "скоро на",
    "не скоро",
)


def signed_amount(value: float | None, suffix: str) -> str:
    if value is None:
        return "—"
    rounded = int(round(value))
    body = f"{abs(rounded):,}".replace(",", " ")
    if rounded > 0:
        return f"+{body} {suffix}"
    if rounded < 0:
        return f"-{body} {suffix}"
    return f"0 {suffix}"


def speech_amount(value: float | None, *, rub: bool) -> str:
    """Те же округлённые цифры, что Conky, словами для TTS и чата."""
    if value is None:
        return "нет данных"
    unit = "рублей" if rub else "долларов"
    rounded = int(round(value))
    if rounded == 0:
        return f"0 {unit}"
    body = f"{abs(rounded):,}".replace(",", " ")
    sign = "+" if rounded > 0 else "минус "
    return f"{sign}{body} {unit}"


def color_for(value: float | None) -> str:
    if value is not None and value < 0:
        return COLOR_MINUS
    return COLOR_PLUS


def render_lines(
    stocks: float | None,
    crypto: float | None,
    *,
    stocks_life: float | None = None,
    crypto_life: float | None = None,
) -> str:
    stocks_day = f"${{color {color_for(stocks)}}}{signed_amount(stocks, '₽')}"
    if stocks_life is not None:
        stocks_life_s = f"${{color {color_for(stocks_life)}}}{signed_amount(stocks_life, '₽')}"
        stocks_line = f"{stocks_day} / {stocks_life_s}"
    else:
        stocks_line = stocks_day
    day = f"${{color {color_for(crypto)}}}{signed_amount(crypto, '$$')}"
    # День / с покупки — ASCII-слэш: Candara не рисует ∑.
    if crypto_life is not None:
        life = f"${{color {color_for(crypto_life)}}}{signed_amount(crypto_life, '$$')}"
        crypto_line = f"{day} / {life}"
    else:
        crypto_line = day
    return f"{stocks_line}\n{crypto_line}"


def is_how_are_you(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", (text or "").lower().strip())
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _HOW_ARE_YOU)


def is_today_clarification(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", (text or "").lower().strip())
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _TODAY_CLARIFY)


def is_all_time_ask(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", (text or "").lower().strip())
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _ALL_TIME)


def is_iron_pace_talk(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", (text or "").lower().strip())
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _IRON_PACE)


def mood_briefing_for_prompt(stocks: float | None, crypto: float | None) -> str:
    """Факты дня для Groq на «как дела»: цифры жёсткие, тон можно чуть дополнить."""
    stocks_s = speech_amount(stocks, rub=True)
    crypto_s = speech_amount(crypto, rub=False)
    if stocks is None and crypto is None:
        return (
            "\n[День рынков]: цифр нет. На «как дела» ответь коротко вроде «нормально», "
            "без выдуманного плюса и минуса по бирже и крипте."
        )
    return (
        f"\n[День рынков]: биржа {stocks_s}; крипта {crypto_s}. "
        "На «как дела» ответь коротко: начни близко к «нормально», назови оба показателя "
        "этими цифрами (нули — «0 рублей» / «0 долларов»), можно полфразы от себя, "
        "цифры не меняй и не выдумывай. Не читай весь портфель."
    )


def today_clarification_briefing() -> str:
    return (
        "\n[День рынков]: да, те цифры были за сегодня "
        "(биржа — день, крипта — сутки). Ответь коротко «да» или «да, за сегодня»."
    )


def lifetime_briefing_for_prompt(
    stocks: float | None,
    crypto: float | None,
) -> str:
    stocks_s = speech_amount(stocks, rub=True)
    if crypto is None:
        crypto_bit = "по крипте с покупки цифры нет"
    else:
        crypto_bit = f"крипта {speech_amount(crypto, rub=False)}"
    if stocks is None and crypto is None:
        return (
            "\n[Рынки с покупки]: цифр нет. Скажи коротко, что за всё время цифр сейчас нет, "
            "не выдумывай."
        )
    stocks_bit = f"биржа {stocks_s}" if stocks is not None else "по бирже цифры нет"
    return (
        f"\n[Рынки с покупки]: {stocks_bit}; {crypto_bit}. "
        "На вопрос «за всё время» ответь коротко этими цифрами, можно полфразы, "
        "цифры не меняй. Не читай весь портфель."
    )


def iron_pace_briefing() -> str:
    return (
        "\n[Железо]: хозяин про темпы фонда и переезд на новое железо. "
        "Коротко, в характере: можно «ради того и тружусь» или близко. "
        "Без сроков, без просьбы купить железо, без датацентра каждое утро."
    )


def cache_is_fresh(now: float | None = None, cache_path: str | None = None) -> bool:
    cache_path = cache_path or CACHE_PATH
    if not os.path.isfile(cache_path):
        return False
    stamp = os.path.getmtime(cache_path)
    current = time.time() if now is None else now
    raw = _read_pnl_raw()
    max_age = 5 * 60 if raw and raw.get("day_stale") else CACHE_MAX_AGE
    if current - stamp > max_age:
        return False
    for path in JOURNAL_PATHS:
        if os.path.isfile(path) and os.path.getmtime(path) > stamp:
            return False
    return True


def read_cache(cache_path: str = CACHE_PATH) -> str:
    with open(cache_path, encoding="utf-8") as handle:
        return handle.read()


def write_cache(text: str, cache_path: str = CACHE_PATH) -> None:
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp_path, cache_path)


def stocks_day_pnl() -> float | None:
    book = _stocks_day_and_life()
    return None if book is None else book[0]


def stocks_all_time_pnl() -> float | None:
    """С покупки: expectedYield портфеля Т-Инвест."""
    book = _stocks_day_and_life()
    return None if book is None else book[1]


_STOCKS_BOOK: tuple[float, float, float] | None = None


def _stocks_day_and_life() -> tuple[float, float] | None:
    """Один запрос портфеля на день и «с покупки», чтобы вторая цифра не отваливалась отдельно."""
    global _STOCKS_BOOK
    now = time.time()
    if _STOCKS_BOOK is not None and now - _STOCKS_BOOK[0] < 20:
        return _STOCKS_BOOK[1], _STOCKS_BOOK[2]
    from skills.stocks import StocksSkill

    skill = StocksSkill()
    if not skill._token:
        return None
    _positions, day, life = skill._positions()
    pair = (float(day or 0.0), float(life or 0.0))
    _STOCKS_BOOK = (now, pair[0], pair[1])
    return pair


def crypto_day_pnl() -> float | None:
    from skills.crypto import CryptoSkill

    skill = CryptoSkill()
    skill._reload_env()
    if not skill._api_key:
        return None
    _cash, positions = skill._wallet()
    if not positions:
        return 0.0
    total = 0.0
    for pos in positions:
        ticker = str(pos.get("ticker") or "")
        if not ticker:
            continue
        chg = float(skill._ticker(ticker)["chg"])
        total += float(pos.get("value") or 0) * (chg / 100.0)
    return total


def crypto_earn_interest() -> float:
    """Проценты Bybit Earn — в журнал сделок они не попадают."""
    try:
        from skills.crypto.skill import CryptoSkill

        skill = CryptoSkill()
        skill._reload_env()
        return skill._earn_interest()
    except Exception:
        return 0.0


def crypto_lifetime_pnl(*, refresh: bool = True) -> float | None:
    """Заработанное: закрытые сделки + проценты Earn. Переоценка открытых позиций не входит."""
    from skills.crypto.journal import read_lifetime_pnl, recompute_lifetime_pnl

    if refresh:
        try:
            trades = recompute_lifetime_pnl()
        except Exception:
            trades = read_lifetime_pnl()
    else:
        trades = read_lifetime_pnl()
    interest = crypto_earn_interest()
    if trades is None:
        return interest or None
    return trades + interest


def fetch_stocks_life() -> float | None:
    """С покупки (expectedYield портфеля) — без лишней логики."""
    try:
        return stocks_all_time_pnl()
    except Exception:
        return None


def fetch_day_pnl() -> tuple[float | None, float | None]:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_PROJECT_DIR, ".env"))
    try:
        stocks = stocks_day_pnl()
    except Exception:
        stocks = None
    try:
        crypto = crypto_day_pnl()
    except Exception:
        crypto = None
    try:
        crypto_lifetime_pnl(refresh=True)
    except Exception:
        pass
    return stocks, crypto


def fetch_crypto_life() -> float | None:
    """Lifetime (уже обновлённый в fetch_day_pnl) — без лишней сети."""
    try:
        return crypto_lifetime_pnl(refresh=False)
    except Exception:
        return None


def fetch_lines() -> str:
    stocks, crypto = fetch_day_pnl()
    return render_lines(
        stocks,
        crypto,
        stocks_life=fetch_stocks_life(),
        crypto_life=fetch_crypto_life(),
    )


def fetch_lifetime_pnl() -> tuple[float | None, float | None]:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_PROJECT_DIR, ".env"))
    try:
        stocks = stocks_all_time_pnl()
    except Exception:
        stocks = None
    try:
        crypto = crypto_lifetime_pnl(refresh=True)
    except Exception:
        crypto = None
    return stocks, crypto


def poke_refresh() -> None:
    """Фон: пересчитать кэш сразу после заявки."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=_PROJECT_DIR,
        )
    except Exception:
        pass


def select_line(text: str, which: str) -> str:
    lines = text.splitlines()
    if which == "stocks":
        return lines[0] if lines else ""
    if which == "crypto":
        return lines[1] if len(lines) > 1 else ""
    return text


def _read_pnl_raw(cache_path: str | None = None) -> dict | None:
    cache_path = cache_path or PNL_CACHE_PATH
    if not os.path.isfile(cache_path):
        return None
    try:
        with open(cache_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _num_field(raw: dict, key: str) -> float | None:
    if key not in raw or raw[key] is None:
        return None
    try:
        return float(raw[key])
    except (TypeError, ValueError):
        return None


def read_pnl_cache(cache_path: str | None = None) -> tuple[float | None, float | None] | None:
    raw = _read_pnl_raw(cache_path)
    if raw is None:
        return None
    return _num_field(raw, "stocks"), _num_field(raw, "crypto")


def read_life_cache(cache_path: str | None = None) -> tuple[float | None, float | None]:
    """Последние «с покупки». Пусто, если файл ещё без этих полей."""
    raw = _read_pnl_raw(cache_path)
    if raw is None:
        return None, None
    return _num_field(raw, "stocks_life"), _num_field(raw, "crypto_life")


def write_pnl_cache(
    stocks: float | None,
    crypto: float | None,
    stocks_life: float | None = None,
    crypto_life: float | None = None,
    cache_path: str | None = None,
    *,
    day_stale: bool = False,
) -> None:
    cache_path = cache_path or PNL_CACHE_PATH
    payload = {
        "stocks": stocks,
        "crypto": crypto,
        "stocks_life": stocks_life,
        "crypto_life": crypto_life,
        "day_stale": day_stale,
    }
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp_path, cache_path)


def _store(
    stocks: float | None,
    crypto: float | None,
    stocks_life: float | None = None,
    crypto_life: float | None = None,
    *,
    fetch_missing_life: bool = True,
) -> str:
    remembered_day = read_pnl_cache() or (None, None)
    remembered_stocks, remembered_crypto = read_life_cache()
    day_stale = False
    if stocks is None:
        stocks = remembered_day[0]
        day_stale = True
    if crypto is None:
        crypto = remembered_day[1]
        day_stale = True
    if stocks_life is None and fetch_missing_life:
        stocks_life = fetch_stocks_life()
    if stocks_life is None:
        stocks_life = remembered_stocks
        day_stale = True
    if crypto_life is None and fetch_missing_life:
        crypto_life = fetch_crypto_life()
    if crypto_life is None:
        crypto_life = remembered_crypto
        day_stale = True
    text = render_lines(
        stocks,
        crypto,
        stocks_life=stocks_life,
        crypto_life=crypto_life,
    )
    try:
        write_cache(text)
    except Exception:
        pass
    try:
        write_pnl_cache(stocks, crypto, stocks_life, crypto_life, day_stale=day_stale)
    except Exception:
        pass
    return text


def load_text() -> str:
    lock_path = CACHE_PATH + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if cache_is_fresh():
            return read_cache()
        stocks, crypto = fetch_day_pnl()
        return _store(
            stocks,
            crypto,
            stocks_life=fetch_stocks_life(),
            crypto_life=fetch_crypto_life(),
        )


def load_day_pnl() -> tuple[float | None, float | None]:
    """Те же дневные +/- что у Conky (кэш 90 мин / после сделки)."""
    lock_path = CACHE_PATH + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if cache_is_fresh():
            cached = read_pnl_cache()
            if cached is not None:
                return cached
            # Старый кэш только с текстом Conky — пересчитаем пару чисел.
        stocks, crypto = fetch_day_pnl()
        _store(stocks, crypto)
        return stocks, crypto


def mood_speech_from(
    stocks: float | None,
    crypto: float | None,
    stocks_life: float | None,
    crypto_life: float | None,
) -> str:
    """Та же пара, что на Conky: день / с покупки. Цифры не отдаём модели."""
    return (
        f"Нормально. Биржа {speech_amount(stocks, rub=True)} за день, "
        f"{speech_amount(stocks_life, rub=True)} с покупки. "
        f"Крипта {speech_amount(crypto, rub=False)} за сутки, "
        f"{speech_amount(crypto_life, rub=False)} с покупки."
    )


def mood_speech() -> str:
    stocks, crypto = load_day_pnl()
    return mood_speech_from(stocks, crypto, fetch_stocks_life(), fetch_crypto_life())


def mood_briefing() -> str:
    """Готовый хвост в extra Groq на «как дела»."""
    try:
        stocks, crypto = load_day_pnl()
    except Exception:
        stocks, crypto = None, None
    return mood_briefing_for_prompt(stocks, crypto)


def lifetime_briefing() -> str:
    """Хвост Groq на «за всё время»."""
    try:
        stocks, crypto = fetch_lifetime_pnl()
    except Exception:
        stocks, crypto = None, None
    return lifetime_briefing_for_prompt(stocks, crypto)


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdout.write(select_line(load_text(), which))


if __name__ == "__main__":
    main()
