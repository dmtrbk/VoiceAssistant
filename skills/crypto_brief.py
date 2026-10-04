# skills/crypto_brief.py
# Голосовой разбор счёта Bybit: вход, выход, исключения, кэш. Заявок не ставит.

from __future__ import annotations

import logging

from skills.base import BaseSkill, RequestContext
from skills.crypto.common import (
    _EXCLUDED_PATH,
    _format_pct,
    _format_usd,
    _read_ticker_set,
    _spoken,
    read_btc_dip,
)
from skills.crypto.desk_policy import (
    ALT_EXIT_MIN_GAIN_PCT,
    BTC_DIP_EXIT_PCT,
    BTC_DIP_TICKER,
    DESK_ALT_SLEEVE,
)
from skills.crypto.journal import open_avg_costs, read_lifetime_pnl
from skills.text_utils import norm as _norm

logger = logging.getLogger(__name__)

_NAMES = {
    "ADA": "Кардано",
    "APT": "Апт",
    "AVAX": "Авакс",
    "BNB": "Биэнби",
    "DOT": "Полкадот",
    "LINK": "Линк",
    "MNT": "Мантл",
    "NEAR": "Ниар",
    "SUI": "Суи",
}


def _name(ticker: str) -> str:
    spoken = _spoken(ticker)
    if spoken != ticker.upper():
        return spoken
    return _NAMES.get(ticker.upper(), spoken)


_HINTS = (
    "разбери крипт",
    "разбор крипт",
    "разложи крипт",
    "подробный отч",
    "подробно по крипт",
    "подробно о крипт",
)


def wants_crypto_brief(text: str) -> bool:
    text = _norm(text)
    if "крипт" not in text:
        return False
    return any(hint in text for hint in _HINTS)


def _gap_pct(price: float, target: float) -> float:
    if price <= 0 or target <= 0:
        return 0.0
    return (target / price - 1.0) * 100.0


def _pnl_pct(price: float, entry: float) -> float | None:
    if price <= 0 or entry <= 0:
        return None
    return (price / entry - 1.0) * 100.0


def build_crypto_brief(skill) -> str:
    """Текст разбора. skill — CryptoSkill с кошельком и полосами."""
    skill._reload_env()
    cash_all, positions = skill._wallet(with_earn=True)
    cash_spot, _rest = skill._wallet(with_earn=False)
    staked = max(0.0, float(cash_all) - float(cash_spot))
    equity = float(cash_all) + sum(float(item.get("value") or 0) for item in positions)
    parts = [f"На счёте {_format_usd(equity)}."]
    if staked >= 1:
        parts.append(
            f"Под проценты {_format_usd(staked)}, свободно {_format_usd(cash_spot)}."
        )
    elif cash_all:
        parts.append(f"Кэш {_format_usd(cash_all)}.")

    costs = open_avg_costs()
    dip = read_btc_dip()
    dip_qty = float(dip.get("qty") or 0)
    dip_entry = float(dip.get("entry") or 0)
    holds = {str(ticker).upper() for ticker in getattr(skill, "_manual_holds", set())}
    excluded = _read_ticker_set(_EXCLUDED_PATH) or set()
    dust: list[str] = []

    for item in positions:
        ticker = str(item.get("ticker") or "").upper()
        value = float(item.get("value") or 0)
        price = float(item.get("price") or 0)
        name = _name(ticker)
        if value < 1:
            dust.append(name)
            continue
        entry = float(costs.get(ticker) or 0)
        pocket = ticker == BTC_DIP_TICKER and dip_qty > 0 and dip_entry > 0
        if pocket:
            entry = dip_entry
        pnl = _pnl_pct(price, entry)
        sentence = f"{name} {_format_usd(value)}"
        if pnl is not None:
            sentence += f", {_format_pct(pnl)} от входа"
        if ticker in holds:
            sentence += ". Твоя позиция, стол её не продаёт"
        elif pocket:
            target = entry * (1.0 + BTC_DIP_EXIT_PCT / 100.0)
            if price + 1e-9 < target:
                sentence += (
                    ". До выхода кармана осталось "
                    + _format_pct(_gap_pct(price, target)).replace("плюс ", "")
                )
            else:
                sentence += ". Карман уже на выходе"
        elif ticker in DESK_ALT_SLEEVE:
            if ticker in excluded:
                sentence += ". Новая покупка закрыта, эта позиция ещё держится"
            bands = None
            try:
                bands = skill._alt_bands(ticker)
            except Exception:
                bands = None
            ma = float(bands["ma"]) if bands and bands.get("ma") else 0.0
            if ma > 0 and price < ma:
                sentence += (
                    ". До средней осталось "
                    + _format_pct(_gap_pct(price, ma)).replace("плюс ", "")
                )
            elif ma > 0 and pnl is not None and pnl >= ALT_EXIT_MIN_GAIN_PCT:
                sentence += ". У средней, стол может продать"
        parts.append(sentence + ".")

    if len(dust) == 1:
        parts.append(f"Пыль: {dust[0]}.")
    elif dust:
        parts.append("Ещё пыль по нескольким монетам.")
    if dip_qty <= 0:
        parts.append("Карман пуст. Ждёт, когда солана просядет за сутки.")
    if excluded:
        names = ", ".join(_name(ticker) for ticker in sorted(excluded))
        parts.append(f"Новые покупки закрыты для: {names}.")
    life = read_lifetime_pnl()
    if life is not None:
        parts.append(f"Закрыто по журналу {_format_usd(life, signed=True)}.")
    return " ".join(parts)


class CryptoBriefSkill(BaseSkill):
    """Разбор счёта. Курсы и сделки остаются у навыка «Крипта»."""

    def can_handle(self, context: RequestContext) -> bool:
        return wants_crypto_brief(context.raw_text)

    def execute(self, context: RequestContext) -> None:
        speak = context.speak
        if speak is None:
            return
        try:
            from skills.crypto.skill import CryptoSkill

            speak(build_crypto_brief(CryptoSkill()))
        except Exception as exc:
            logger.error("[Разбор крипты] %s", exc)
            speak("Разбор крипты не вышел.")
