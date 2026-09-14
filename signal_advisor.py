#!/usr/bin/env python3
# signal_advisor.py
# Отдельный советник: активные BUY-сигналы Т-Инвест → Groq → текст.
# Заявки не ставит. Стол Джарвиса торгует сам; это только рекомендация хозяину.
#
#   .venv/bin/python signal_advisor.py
#   .venv/bin/python signal_advisor.py --stdout

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT_DIR, ".env"))

from skills.stocks import (  # noqa: E402
    StocksSkill,
    _MOEX_BOARDS,
    _buy_block_reason,
    _signal_weight,
    format_journal,
)
from skills.utils import send_telegram_notification, telegram_configured  # noqa: E402

logger = logging.getLogger("signal_advisor")

_SIGNAL_LIMIT = 12
_SYSTEM = (
    "Ты Джарвис, советник фонда модернизации: прибыль идёт на модели ИИ и новое железо. "
    "Хозяин читает текст в Telegram. Заявки сам не ставишь — только совет. "
    "Бумаги бери строго из списка официальных сигналов Т-Инвест, ничего не выдумывай. "
    "Выбери до четырёх имён. Если у бумаги пометка «купи сам» — так и скажи, через API её нет. "
    "Если цена ниже закрытия 10 торговых дней назад — не советуй докупать без оговорки. "
    "Если выше — это в пользу сигнала. "
    "Не обещай прибыль и не зови в плечо. Коротко, по делу, до 900 знаков. "
    "О себе только в мужском роде. Можно короткий список, без markdown-заголовков."
)


def collect_signal_rows(skill: StocksSkill, limit: int = _SIGNAL_LIMIT) -> list[dict[str, Any]]:
    ranked = sorted(
        skill._fetch_buy_signals(),
        key=lambda sig: _signal_weight(sig.get("probability")),
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sig in ranked:
        if len(rows) >= limit:
            break
        uid = str(sig.get("instrumentUid") or sig.get("instrument_uid") or "").strip()
        if not uid:
            continue
        inst = skill._instrument(None, None, uid=uid)
        ticker = str(inst.get("ticker") or "").upper()
        if not ticker or ticker in seen:
            continue
        class_code = str(inst.get("classCode") or "")
        if class_code and class_code not in _MOEX_BOARDS:
            continue
        kind = str(inst.get("instrumentKind") or inst.get("instrumentType") or "").upper()
        if kind and "SHARE" not in kind and "ETF" not in kind:
            continue
        seen.add(ticker)
        name = skill._spoken_name(ticker, inst)
        skill._remember_name(ticker, name)
        blocked = skill._is_buy_blocked(ticker) or inst.get("apiTradeAvailableFlag") is False
        try:
            mom = skill._momentum_10d(ticker)
        except Exception:
            mom = {"close": 0.0, "close_10": 0.0, "chg_10": None, "trend": "нет данных"}
        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "probability": _signal_weight(sig.get("probability")),
                "strategy": str(sig.get("strategyName") or sig.get("strategyType") or "").strip(),
                "info": str(sig.get("info") or sig.get("name") or "").strip(),
                "blocked": blocked,
                "reason": _buy_block_reason(ticker) if blocked else "",
                "trend": mom.get("trend") or "нет данных",
                "chg_10": mom.get("chg_10"),
            }
        )
    return rows


def collect_book(skill: StocksSkill) -> str:
    positions, _day, _total = skill._safe_positions()
    cash = 0.0
    try:
        cash = float(skill._broker_cash() or 0)
    except Exception:
        cash = 0.0
    if not positions and cash <= 0:
        return "портфель пуст или недоступен"
    parts = [f"кэш {cash:.0f} ₽"]
    for row in positions[:8]:
        ticker = str(row.get("ticker") or "").upper()
        qty = row.get("qty") or 0
        name = skill._spoken_name(ticker)
        parts.append(f"{name} ({ticker}) {qty:g}")
    return "; ".join(parts)


def build_facts(rows: list[dict[str, Any]], book: str) -> str:
    lines = [f"Портфель: {book}", "Сигналы BUY Т-Инвест, сильнее выше:"]
    if not rows:
        lines.append("активных сигналов нет")
        return "\n".join(lines)
    for index, row in enumerate(rows, start=1):
        mark = f" — купи сам ({row['reason']})" if row.get("blocked") else ""
        extra = row.get("strategy") or row.get("info") or ""
        tail = f", {extra}" if extra else ""
        trend = str(row.get("trend") or "")
        chg = row.get("chg_10")
        if trend == "выше" and chg is not None:
            mom = f", выше закрытия 10д ({chg:+.1f}%)"
        elif trend == "ниже" and chg is not None:
            mom = f", ниже закрытия 10д ({chg:+.1f}%)"
        elif trend and trend != "нет данных":
            mom = f", {trend} закрытия 10д"
        else:
            mom = ""
        lines.append(
            f"{index}. {row['name']} ({row['ticker']}) вероятность {row['probability']:.0f}{tail}{mom}{mark}"
        )
    return "\n".join(lines)


def _fallback_advice(facts: str) -> str:
    return "ИИ сейчас молчит. Сырые сигналы:\n" + facts


def advise(facts: str) -> str:
    try:
        from skill_settings import get_effective_groq_model
        from skills.groq_client import complete

        model = get_effective_groq_model()
        text = complete(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": facts + "\n\nДай рекомендацию хозяину."},
            ],
            preferred=model,
            temperature=0.3,
            max_tokens=400,
        ).strip()
        if text:
            return text
    except Exception as exc:
        logger.warning("[Советник] Groq: %s", exc)
    return _fallback_advice(facts)


def format_momentum(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        trend = str(row.get("trend") or "нет данных")
        chg = row.get("chg_10")
        name = row.get("name") or row.get("ticker")
        ticker = row.get("ticker")
        if trend == "нет данных" or chg is None:
            continue
        flag = "осторожно" if trend == "ниже" else "в плюсе"
        lines.append(f"{name} ({ticker}): {trend} закрытия 10д ({chg:+.1f}%) — {flag}")
    if not lines:
        return ""
    return "Моментум 10 дней:\n" + "\n".join(lines)


def compose_message(advice: str, rows: list[dict[str, Any]]) -> str:
    parts = [advice.strip()]
    mom = format_momentum(rows)
    if mom:
        parts.append(mom)
    journal = format_journal()
    if journal:
        parts.append(journal)
    return "\n\n".join(part for part in parts if part)


def deliver(text: str, to_telegram: bool, to_stdout: bool = True) -> None:
    if to_stdout:
        print(text)
    if to_telegram:
        send_telegram_notification(text, background=False)


def run(
    skill: StocksSkill | None = None,
    to_telegram: bool | None = None,
    to_stdout: bool = True,
) -> str:
    skill = skill or StocksSkill()
    if not skill._token:
        raise RuntimeError("нет токена Т-Инвест")
    rows = collect_signal_rows(skill)
    facts = build_facts(rows, collect_book(skill))
    text = compose_message(advise(facts), rows)
    if to_telegram is None:
        to_telegram = telegram_configured()
    deliver(text, to_telegram=bool(to_telegram), to_stdout=to_stdout)
    return text


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Рекомендации по сигналам Т-Инвест. Заявки не ставит.")
    parser.add_argument("--stdout", action="store_true", help="только печать, без Telegram")
    parser.add_argument("--telegram", action="store_true", help="обязательно отправить в Telegram")
    args = parser.parse_args(argv)
    to_telegram: bool | None
    if args.stdout and args.telegram:
        print("Укажи либо --stdout, либо --telegram.", file=sys.stderr)
        return 2
    if args.stdout:
        to_telegram = False
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
        logger.error("[Советник] %s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
