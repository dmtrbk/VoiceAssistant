# skills/crypto/desk_policy.py
# Правила автостола: режим рынка, скоринг, коридор, фильтры. Без сети и Groq.

from __future__ import annotations

import time
from typing import Any

# Ядро whitelist для авто (плюс уже держанные позиции).
DESK_CORE = frozenset({"BTC", "ETH", "SOL"})
DESK_MAX_NAMES = 3
DESK_CASH_FLOOR_PCT = 25.0
DESK_CASH_FLOOR_BULL_PCT = 8.0
REBALANCE_BAND_PCT = 6.0
REBALANCE_BAND_CORE_PCT = 4.0
REBALANCE_BAND_ALT_PCT = 8.0
BTC_RISK_OFF_7D = -5.0
BTC_RISK_OFF_DAY = -3.0
BTC_BULL_7D = 5.0
# Ядро: лёгкий mean-reversion на откате 7д; альты — только неотрицательный импульс.
CORE_CHG7_FLOOR = -8.0
MIN_TURNOVER_USD = 5_000_000.0
MAX_DAY_PUMP_PCT = 25.0
# Уже держанное: при сильном дневном пампе фиксируем избыток даже внутри коридора.
TAKE_PROFIT_DAY_PCT = 18.0
# Дозор (~12 мин): чуть чувствительнее TP, добор только ядра на просадке.
WATCH_TP_DAY_PCT = 12.0
WATCH_DIP_DAY_PCT = -5.0
WATCH_DIP_BUY_FRAC = 0.5
WATCH_MAX_TRADES_PER_HOUR = 2
# Трейлинг-стоп дозора: защита пика с покупки (только позиции стола).
TRAIL_ARM_PCT = 5.0
TRAIL_CORE_PCT = 6.0
TRAIL_ALT_PCT = 8.0
CHURN_COOLDOWN_HOURS = 12.0


def is_risk_off(*, btc_chg_7: float | None, btc_chg_day: float | None) -> bool:
    """Общий медвежий фон по BTC → только кэш."""
    if btc_chg_7 is not None and btc_chg_7 <= BTC_RISK_OFF_7D:
        return True
    if (
        btc_chg_7 is not None
        and btc_chg_7 < 0
        and btc_chg_day is not None
        and btc_chg_day <= BTC_RISK_OFF_DAY
    ):
        return True
    return False


def cash_floor_pct(*, btc_chg_7: float | None, btc_chg_day: float | None) -> float:
    """Доля кэша: bull ниже, норма 25%, risk-off — 100% (вызывающий обычно уже в кэш)."""
    if is_risk_off(btc_chg_7=btc_chg_7, btc_chg_day=btc_chg_day):
        return 100.0
    if btc_chg_7 is not None and btc_chg_7 >= BTC_BULL_7D:
        if btc_chg_day is None or btc_chg_day > -1.0:
            return DESK_CASH_FLOOR_BULL_PCT
    return DESK_CASH_FLOOR_PCT


def band_pct_for(ticker: str) -> float:
    """Уже коридор для ядра, шире для альтов — меньше шума по TON и т.п."""
    if str(ticker or "").upper() in DESK_CORE:
        return REBALANCE_BAND_CORE_PCT
    return REBALANCE_BAND_ALT_PCT


def _chg7_allowed(ticker: str, chg7: Any) -> bool:
    if chg7 is None:
        return True
    floor = CORE_CHG7_FLOOR if str(ticker or "").upper() in DESK_CORE else 0.0
    return float(chg7) >= floor


def filter_auto_candidates(
    rows: list[dict[str, Any]],
    *,
    held: set[str],
    watchlist: list[str],
    cooldown: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Узкий список для автостола: ядро + watch + позиции, без пампов и кулдауна."""
    cool = {str(t).upper() for t in (cooldown or set())}
    held_u = {str(t).upper() for t in held}
    watch_u = [str(t).upper() for t in watchlist]
    allow = DESK_CORE | held_u | set(watch_u)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in seen or ticker not in allow:
            continue
        if ticker in cool and ticker not in held_u:
            continue
        turnover = float(row.get("turnover") or 0)
        chg = float(row.get("chg") or 0)
        # Уже держанное не режем по обороту/пампу — иначе не сможем довести ребаланс.
        if ticker not in held_u:
            if turnover > 0 and turnover < MIN_TURNOVER_USD and ticker not in DESK_CORE:
                continue
            if chg >= MAX_DAY_PUMP_PCT:
                continue
        seen.add(ticker)
        out.append(row)
    return out


def _row_score(row: dict[str, Any]) -> float:
    chg7 = row.get("chg_7")
    chg = float(row.get("chg") or 0)
    score = 0.0
    if chg7 is not None:
        score += float(chg7) * 1.5
    score += chg * 0.5
    # Лёгкий бонус ядру за стабильность ликвидности.
    ticker = str(row.get("ticker") or "").upper()
    if ticker in DESK_CORE:
        score += 2.0
    return score


def score_alloc(
    candidates: list[dict[str, Any]],
    *,
    max_names: int = DESK_MAX_NAMES,
    cash_floor_pct: float = DESK_CASH_FLOOR_PCT,
) -> tuple[dict[str, float], str]:
    """Доли без LLM. Пустой alloc = кэш. Сумма долей ≤ 100 − cash_floor."""
    floor = max(0.0, min(100.0, float(cash_floor_pct)))
    if floor >= 100.0:
        return {}, "Рынок слабый — держу кэш в тетере."
    ranked = sorted(
        (
            row
            for row in candidates
            if str(row.get("ticker") or "").upper()
            and _chg7_allowed(str(row.get("ticker") or ""), row.get("chg_7"))
        ),
        key=_row_score,
        reverse=True,
    )
    picked = ranked[: max(1, int(max_names))]
    # Только положительный скор или ядро в допустимом 7д-окне (в т.ч. лёгкий откат).
    usable: list[dict[str, Any]] = []
    for row in picked:
        score = _row_score(row)
        ticker = str(row.get("ticker") or "").upper()
        chg7 = row.get("chg_7")
        if score > 0:
            usable.append(row)
            continue
        if ticker in DESK_CORE and _chg7_allowed(ticker, chg7):
            usable.append(row)
    if not usable:
        return {}, "Рынок слабый — держу кэш в тетере."
    invest_pct = max(0.0, 100.0 - floor)
    weights = [max(_row_score(row), 0.5) for row in usable]
    total_w = sum(weights) or 1.0
    alloc = {
        str(row["ticker"]).upper(): round(invest_pct * (w / total_w), 1)
        for row, w in zip(usable, weights)
    }
    # Нормализация суммы долей invest_pct.
    s = sum(alloc.values())
    if s > 0 and abs(s - invest_pct) > 0.2:
        alloc = {k: round(v * invest_pct / s, 1) for k, v in alloc.items()}
    names = ", ".join(f"{k} {int(round(v))}%" for k, v in alloc.items())
    return alloc, f"Скор-стол: {names}, кэш ≥ {int(floor)}%."


def drift_usd(current: float, target: float) -> float:
    return abs(current - target)


def should_rebalance_leg(
    *,
    current_value: float,
    target_value: float,
    equity: float,
    band_pct: float = REBALANCE_BAND_PCT,
    min_trade_usd: float,
    day_chg: float | None = None,
) -> bool:
    """Коридор: не трогаем ногу, пока отклонение меньше max(band% equity, min_trade).

    Take-profit: сильный дневной памп при избытке над целью — фиксируем даже внутри band.
    """
    excess = float(current_value) - float(target_value)
    if (
        day_chg is not None
        and float(day_chg) >= TAKE_PROFIT_DAY_PCT
        and excess >= float(min_trade_usd)
    ):
        return True
    equity = max(float(equity), 1.0)
    band = max(equity * (band_pct / 100.0), float(min_trade_usd))
    return drift_usd(current_value, target_value) >= band


def watch_rate_ok(
    watch_trades: list[float] | None,
    *,
    now: float | None = None,
    max_per_hour: int = WATCH_MAX_TRADES_PER_HOUR,
) -> bool:
    """Не больше max_per_hour сделок дозора за последний час."""
    current = time.time() if now is None else float(now)
    cutoff = current - 3600.0
    recent = [float(ts) for ts in (watch_trades or []) if float(ts) >= cutoff]
    return len(recent) < max(0, int(max_per_hour))


def should_watch_take_profit(
    *,
    day_chg: float | None,
    current_value: float,
    target_value: float,
    min_trade_usd: float,
    tp_pct: float = WATCH_TP_DAY_PCT,
) -> bool:
    excess = float(current_value) - float(target_value)
    if excess < float(min_trade_usd):
        return False
    if day_chg is None:
        return False
    return float(day_chg) >= float(tp_pct)


def should_watch_dip_buy(
    *,
    ticker: str,
    day_chg: float | None,
    current_value: float,
    target_value: float,
    min_trade_usd: float,
    dip_pct: float = WATCH_DIP_DAY_PCT,
) -> bool:
    """Докупка только ядра к сохранённой цели на дневной просадке."""
    if str(ticker or "").upper() not in DESK_CORE:
        return False
    gap = float(target_value) - float(current_value)
    if gap < float(min_trade_usd):
        return False
    if day_chg is None:
        return False
    return float(day_chg) <= float(dip_pct)


def trail_pct_for(ticker: str) -> float:
    if str(ticker or "").upper() in DESK_CORE:
        return TRAIL_CORE_PCT
    return TRAIL_ALT_PCT


def update_trail_leg(
    *,
    price: float,
    entry: float,
    high: float | None = None,
    armed: bool = False,
    arm_pct: float = TRAIL_ARM_PCT,
    trail_pct: float = TRAIL_CORE_PCT,
) -> dict[str, Any]:
    """Обновить водяную марку. hit=True — цена пробила подтягивающийся стоп."""
    px = float(price)
    ent = float(entry) if entry and float(entry) > 0 else px
    if px <= 0 or ent <= 0:
        return {
            "entry": max(ent, 0.0),
            "high": max(float(high or 0), ent, 0.0),
            "armed": False,
            "stop": 0.0,
            "hit": False,
            "gain_pct": 0.0,
        }
    hi = max(float(high or 0), ent, px)
    gain_pct = (px / ent - 1.0) * 100.0
    is_armed = bool(armed) or gain_pct >= float(arm_pct)
    stop = hi * (1.0 - float(trail_pct) / 100.0)
    hit = is_armed and px <= stop + 1e-12
    return {
        "entry": ent,
        "high": hi,
        "armed": is_armed,
        "stop": stop,
        "hit": hit,
        "gain_pct": gain_pct,
    }
