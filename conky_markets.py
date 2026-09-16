#!/usr/bin/env python3
# conky_markets.py
# Две строки для Conky: дневной +/- биржи (Т-Инвест) и крипты (Bybit, 24ч).
# Цвета из ~/.conky: плюс c0c0c0 (system), минус 888888 (comands default).
# Кэш 90 мин, сразу после сделки (дневник новее кэша). Стол заявок не запускает.

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

COLOR_PLUS = "c0c0c0"
COLOR_MINUS = "888888"
CACHE_PATH = os.path.join(_PROJECT_DIR, ".conky_markets.cache")
CACHE_MAX_AGE = 90 * 60
JOURNAL_PATHS = (
    os.path.join(_PROJECT_DIR, "jarvis_crypto_trades.json"),
    os.path.join(_PROJECT_DIR, "jarvis_trades.json"),
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


def color_for(value: float | None) -> str:
    if value is not None and value < 0:
        return COLOR_MINUS
    return COLOR_PLUS


def render_lines(stocks: float | None, crypto: float | None) -> str:
    stocks_line = f"${{color {color_for(stocks)}}}{signed_amount(stocks, '₽')}"
    crypto_line = f"${{color {color_for(crypto)}}}{signed_amount(crypto, '$$')}"
    return f"{stocks_line}\n{crypto_line}"


def cache_is_fresh(now: float | None = None, cache_path: str = CACHE_PATH) -> bool:
    if not os.path.isfile(cache_path):
        return False
    stamp = os.path.getmtime(cache_path)
    current = time.time() if now is None else now
    if current - stamp > CACHE_MAX_AGE:
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
    from skills.stocks import StocksSkill

    skill = StocksSkill()
    if not skill._token:
        return None
    _positions, day_total, _total = skill._positions()
    return float(day_total or 0.0)


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


def fetch_lines() -> str:
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
    return render_lines(stocks, crypto)


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


def load_text() -> str:
    lock_path = CACHE_PATH + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if cache_is_fresh():
            return read_cache()
        text = fetch_lines()
        try:
            write_cache(text)
        except Exception:
            pass
        return text


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdout.write(select_line(load_text(), which))


if __name__ == "__main__":
    main()
