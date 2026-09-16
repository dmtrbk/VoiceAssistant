#!/usr/bin/env python3
# crypto_advisor.py
# ИИ выбирает спотовые монеты Bybit. Заявки не ставит.
#
#   .venv/bin/python crypto_advisor.py
#   .venv/bin/python crypto_advisor.py --stdout

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT_DIR, ".env"))

from skills.crypto import CryptoSkill, format_journal  # noqa: E402
from skills.utils import send_telegram_notification, telegram_configured  # noqa: E402

logger = logging.getLogger("crypto_advisor")


def collect_book(skill: CryptoSkill) -> str:
    if not skill._api_key:
        skill._reload_env()
    if not skill._api_key:
        return "ключа Bybit нет"
    try:
        cash, positions = skill._wallet()
    except Exception:
        return "счёт недоступен"
    parts = [f"кэш {cash:.0f} USDT"]
    for item in positions[:8]:
        parts.append(f"{item['name']} ({item['ticker']}) {item['value']:.0f}")
    return "; ".join(parts)


def compose_message(advice: str, alloc: dict[str, float], rows: list[dict[str, Any]]) -> str:
    parts = [advice.strip()]
    if alloc:
        by_ticker = {str(row.get("ticker") or ""): row for row in rows}
        named = []
        for ticker, pct in alloc.items():
            name = (by_ticker.get(ticker) or {}).get("name") or ticker
            named.append(f"{name} {int(round(pct))}%")
        parts.append("Доли: " + ", ".join(named) + ".")
    mom_lines: list[str] = []
    for row in rows:
        chg = row.get("chg_7")
        if chg is None:
            continue
        flag = "осторожно" if str(row.get("trend") or "") == "ниже" else "в плюсе"
        mom_lines.append(
            f"{row.get('name')} ({row.get('ticker')}): 7д {chg:+.1f}% — {flag}"
        )
    if mom_lines:
        parts.append("Моментум 7 дней:\n" + "\n".join(mom_lines[:8]))
    journal = format_journal()
    if journal:
        parts.append(journal)
    return "\n\n".join(part for part in parts if part)


def run(
    skill: CryptoSkill | None = None,
    to_telegram: bool | None = None,
    to_stdout: bool = True,
) -> str:
    skill = skill or CryptoSkill()
    skill._reload_env()
    rows = skill.desk_candidates()
    alloc, why = skill.ai_pick_alloc(rows)
    if why:
        advice = why
    elif alloc:
        advice = "Я бы держал эти доли на споте, без плеча."
    else:
        advice = "Сейчас лучше кэш в тетере, чем набирать монеты вслепую."
    text = compose_message(advice, alloc, rows)
    if to_telegram is None:
        to_telegram = telegram_configured()
    if to_stdout:
        print(text)
    if to_telegram:
        send_telegram_notification(text, background=False)
    return text


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Совет по крипте Bybit. Заявки не ставит.")
    parser.add_argument("--stdout", action="store_true", help="только печать, без Telegram")
    parser.add_argument("--telegram", action="store_true", help="обязательно отправить в Telegram")
    args = parser.parse_args(argv)
    if args.stdout and args.telegram:
        print("Укажи либо --stdout, либо --telegram.", file=sys.stderr)
        return 2
    if args.stdout:
        to_telegram: bool | None = False
    elif args.telegram:
        if not telegram_configured():
            print("Telegram не настроен.", file=sys.stderr)
            return 2
        to_telegram = True
    else:
        to_telegram = None
    try:
        run(to_telegram=to_telegram)
    except Exception as exc:
        logger.error("[Крипта-советник] %s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
