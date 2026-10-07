# skills/stocks/desk_policy.py
# Правила фондового стола: режим IMOEX, доля кэша, коридор, анти-чёрн, выходы, паркинг. Без сети.

from __future__ import annotations

import math
import time
from datetime import date, timedelta
from typing import Any

# Режим рынка по индексу Мосбиржи. Индекс спокойнее BTC — пороги мягче крипты.
IMOEX_RISK_OFF_WEEK = -4.0
IMOEX_RISK_OFF_DAY = -2.0
IMOEX_BULL_WEEK = 3.0
CASH_FLOOR_PCT = 10.0
CASH_FLOOR_BULL_PCT = 0.0
# Коридор: целевую ногу не трогаем, пока отклонение меньше доли счёта.
REBALANCE_BAND_PCT = 5.0
# Сильный дневной рост: избыток над целью фиксируем даже внутри коридора.
TAKE_PROFIT_DAY_PCT = 7.0
# Сигналы Т-Инвест живут днями — свежую покупку не ротируем сутки.
MIN_HOLD_HOURS = 24.0
# Убыточная продажа или выход по стопу/трейлу — сутки не берём бумагу снова.
COOLDOWN_HOURS = 24.0
EXIT_REASONS = frozenset({"стоп", "трейл"})
WATCH_MAX_TRADES_PER_HOUR = 2
STOP_LOSS_PCT = 10.0
TRAIL_ARM_PCT = 6.0
TRAIL_PCT = 4.0
WATCH_PERIOD_CALM = 15 * 60
WATCH_PERIOD_NEAR = 3 * 60
NEAR_STOP_GAP = 2.0
NEAR_TRAIL_GAP = 1.0
# Паркинг: в фонд уходит кэш сверх буфера, и только если сумма заметная —
# каждая сделка с фондом стоит комиссию и спред.
PARK_BUFFER_PCT = 2.0
PARK_MIN_PCT = 3.0
PARK_MIN_RUB = 500.0


def index_changes(
    history: list[tuple[str, float]],
    current: float | None,
    day_pct: float | None,
    *,
    today: date,
) -> tuple[float | None, float | None]:
    """(неделя %, сутки %) по закрытиям индекса и текущему значению.

    history — пары (YYYY-MM-DD, close). Неделя — к последнему закрытию не позже today − 7 дней.
    """
    rows = sorted(
        ((str(d)[:10], float(c)) for d, c in history if c and float(c) > 0),
        reverse=True,
    )
    now = float(current) if current and float(current) > 0 else None
    if now is None and rows:
        now = rows[0][1]
    if now is None:
        return None, day_pct
    if day_pct is None:
        prev = [c for d, c in rows if d < today.isoformat()]
        if prev:
            day_pct = round((now / prev[0] - 1.0) * 100.0, 2)
    cutoff = (today - timedelta(days=7)).isoformat()
    week = None
    for d, close in rows:
        if d <= cutoff:
            week = round((now / close - 1.0) * 100.0, 2)
            break
    return week, day_pct


def is_risk_off(*, week: float | None, day: float | None) -> bool:
    """Слабый рынок: новых бумаг не берём, купленные держим до своего выхода."""
    if week is not None and week <= IMOEX_RISK_OFF_WEEK:
        return True
    if week is not None and week < 0 and day is not None and day <= IMOEX_RISK_OFF_DAY:
        return True
    return False


def cash_floor_pct(*, week: float | None, day: float | None) -> float:
    if is_risk_off(week=week, day=day):
        return 100.0
    if week is not None and week >= IMOEX_BULL_WEEK and (day is None or day > -1.0):
        return CASH_FLOOR_BULL_PCT
    return CASH_FLOOR_PCT


def apply_cash_floor(alloc: dict[str, float], floor_pct: float) -> dict[str, float]:
    """Доли сигналов сжимаем так, чтобы кэш остался не меньше floor_pct."""
    room = max(0.0, 100.0 - max(0.0, float(floor_pct)))
    total = sum(max(0.0, float(v)) for v in alloc.values())
    if room <= 0 or total <= 0:
        return {}
    scale = min(1.0, room / total)
    return {t: round(float(v) * scale, 1) for t, v in alloc.items() if float(v) > 0}


def should_rebalance_leg(
    *,
    current_value: float,
    target_value: float,
    equity: float,
    min_trade_rub: float,
    band_pct: float = REBALANCE_BAND_PCT,
    day_chg: float | None = None,
    take_profit_pct: float = TAKE_PROFIT_DAY_PCT,
) -> bool:
    """Целевая нога: двигаем, только если ушла за коридор. Дневной памп — режем избыток сразу."""
    excess = float(current_value) - float(target_value)
    if day_chg is not None and float(day_chg) >= take_profit_pct and excess >= float(min_trade_rub):
        return True
    band = max(max(float(equity), 1.0) * band_pct / 100.0, float(min_trade_rub))
    return abs(excess) >= band


def watch_period_sec(
    *,
    legs: list[dict[str, Any]],
    calm: int = WATCH_PERIOD_CALM,
    near: int = WATCH_PERIOD_NEAR,
) -> int:
    """15 мин спокойно; 3 мин, если цена близко к стопу −10% или к трейлу."""
    for leg in legs:
        px = float(leg.get("price") or 0)
        ent = float(leg.get("entry") or 0)
        if px <= 0 or ent <= 0:
            continue
        gain = (px / ent - 1.0) * 100.0
        if gain <= -STOP_LOSS_PCT + NEAR_STOP_GAP + 1e-6:
            return int(near)
        stop = float(leg.get("stop") or 0)
        if leg.get("armed") and stop > 0 and px <= stop * (1.0 + NEAR_TRAIL_GAP / 100.0):
            return int(near)
    return int(calm)


def watch_snapshot_line(
    ticker: str,
    *,
    price: float,
    entry: float,
    high: float,
    armed: bool,
    stop: float,
) -> str:
    """Нога дозора: avg, цена, дистанция до стопа и трейла."""
    name = str(ticker or "?").upper()
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        return f"{name} px={px:.4g} avg=?"
    gain = (px / ent - 1.0) * 100.0
    bits = [f"{name} px={px:.4g} avg={ent:.4g} {gain:+.1f}% до стопа {gain + STOP_LOSS_PCT:.1f}пп"]
    if armed and float(stop or 0) > 0:
        dist = (px / float(stop) - 1.0) * 100.0
        bits.append(f"пик {float(high):.4g} трейл {float(stop):.4g} до него {dist:+.1f}%")
    else:
        bits.append(f"трейл с +{TRAIL_ARM_PCT:.0f}%")
    return " ".join(bits)


def watch_rate_ok(
    watch_trades: list[float] | None,
    *,
    now: float | None = None,
    max_per_hour: int = WATCH_MAX_TRADES_PER_HOUR,
) -> bool:
    current = time.time() if now is None else float(now)
    recent = [float(ts) for ts in (watch_trades or []) if float(ts) >= current - 3600.0]
    return len(recent) < max(0, int(max_per_hour))


def update_trail_leg(
    *,
    price: float,
    entry: float,
    high: float | None = None,
    armed: bool = False,
    arm_pct: float = TRAIL_ARM_PCT,
    trail_pct: float = TRAIL_PCT,
) -> dict[str, Any]:
    """Водяная марка. hit=True — цена пробила подтянутый стоп после взвода."""
    px = float(price or 0)
    ent = float(entry) if entry and float(entry) > 0 else px
    if px <= 0 or ent <= 0:
        return {"entry": max(ent, 0.0), "high": max(float(high or 0), ent, 0.0),
                "armed": False, "stop": 0.0, "hit": False, "gain_pct": 0.0}
    hi = max(float(high or 0), ent, px)
    gain_pct = (px / ent - 1.0) * 100.0
    is_armed = bool(armed) or gain_pct >= float(arm_pct)
    stop = hi * (1.0 - float(trail_pct) / 100.0)
    return {
        "entry": ent,
        "high": hi,
        "armed": is_armed,
        "stop": stop,
        "hit": is_armed and px <= stop + 1e-12,
        "gain_pct": gain_pct,
    }


def exit_reason(*, price: float, entry: float, trail_hit: bool) -> str | None:
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        return None
    if (px / ent - 1.0) * 100.0 <= -STOP_LOSS_PCT:
        return "стоп"
    if trail_hit:
        return "трейл"
    return None


def park_amount_rub(*, cash: float, equity: float) -> float:
    """Сколько свободного кэша положить в фонд. 0 — мелочь, не стоит комиссии."""
    eq = max(float(equity or 0), 0.0)
    amount = float(cash or 0) - eq * PARK_BUFFER_PCT / 100.0
    if amount < max(eq * PARK_MIN_PCT / 100.0, PARK_MIN_RUB):
        return 0.0
    return amount


def park_release_lots(*, need_rub: float, cash: float, lot_cost: float, held_lots: int) -> int:
    """Сколько лотов фонда продать, чтобы хватило на покупки."""
    gap = float(need_rub) - float(cash)
    if gap <= 0 or lot_cost <= 0 or held_lots <= 0:
        return 0
    return min(int(held_lots), int(math.ceil(gap / float(lot_cost))))
