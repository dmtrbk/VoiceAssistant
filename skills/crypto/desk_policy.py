# skills/crypto/desk_policy.py
# Правила автостола: режим рынка, скоринг, коридор, фильтры. Без сети и Groq.

from __future__ import annotations

import time
from typing import Any

# Ядро whitelist для авто (плюс уже держанные позиции).
DESK_CORE = frozenset({"BTC", "ETH"})
# Узкий spot-рукав альта поверх ядра: ≤ ALT_SLEEVE_MAX_PCT, недобор → кэш.
# Бывшая доля SOL (~⅓ старого ядра) ушла сюда: 15% + ~20% ≈ 35%.
DESK_ALT_SLEEVE = frozenset({
    "XRP", "DOGE", "LINK", "AVAX", "TON", "SUI", "NEAR", "ADA", "MNT", "BNB", "APT", "DOT",
})
DESK_MAX_NAMES = 2
ALT_SLEEVE_MAX_PCT = 35.0
ALT_MAX_NAMES = 3
DESK_CASH_FLOOR_PCT = 25.0
DESK_CASH_FLOOR_BULL_PCT = 8.0
REBALANCE_BAND_PCT = 6.0
REBALANCE_BAND_CORE_PCT = 4.0
REBALANCE_BAND_ALT_PCT = 8.0
BTC_RISK_OFF_7D = -5.0
BTC_RISK_OFF_DAY = -3.0
BTC_BULL_7D = 5.0
# Ядро: лёгкий mean-reversion на откате 7д.
CORE_CHG7_FLOOR = -8.0
# Альты: mean-reversion — берём просадку, не разгон; потолок 7д отсекает уже выросшее.
ALT_CHG7_FLOOR = -18.0
ALT_CHG7_CEIL = 8.0
ALT_ENTRY_MAX_DAY_PCT = 2.0  # новые альты не берём, если сутки уже в плюсе сильно
MIN_TURNOVER_USD = 5_000_000.0
MAX_DAY_PUMP_PCT = 25.0
# Уже держанное: при сильном дневном пампе фиксируем избыток даже внутри коридора.
TAKE_PROFIT_DAY_PCT = 18.0
TAKE_PROFIT_ALT_DAY_PCT = 12.0  # альты раньше фиксируем на подъёме
# Дозор (~12 мин): TP / докуп на просадке (ядро + рукав альта).
WATCH_TP_DAY_PCT = 12.0
WATCH_TP_ALT_DAY_PCT = 8.0
WATCH_DIP_DAY_PCT = -5.0
WATCH_DIP_ALT_DAY_PCT = -4.0
WATCH_DIP_BUY_FRAC = 0.5
WATCH_MAX_TRADES_PER_HOUR = 2
# Трейлинг-стоп дозора: защита пика с покупки (только позиции стола).
TRAIL_ARM_PCT = 5.0
TRAIL_CORE_PCT = 6.0
TRAIL_ALT_PCT = 8.0
CHURN_COOLDOWN_HOURS = 12.0


def is_alt_sleeve(ticker: str) -> bool:
    return str(ticker or "").upper() in DESK_ALT_SLEEVE


def take_profit_day_pct_for(ticker: str) -> float:
    return TAKE_PROFIT_ALT_DAY_PCT if is_alt_sleeve(ticker) else TAKE_PROFIT_DAY_PCT


def watch_tp_day_pct_for(ticker: str) -> float:
    return WATCH_TP_ALT_DAY_PCT if is_alt_sleeve(ticker) else WATCH_TP_DAY_PCT


def watch_dip_day_pct_for(ticker: str) -> float:
    return WATCH_DIP_ALT_DAY_PCT if is_alt_sleeve(ticker) else WATCH_DIP_DAY_PCT



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
    c = float(chg7)
    t = str(ticker or "").upper()
    if t in DESK_CORE:
        return c >= CORE_CHG7_FLOOR
    if t in DESK_ALT_SLEEVE:
        return ALT_CHG7_FLOOR <= c <= ALT_CHG7_CEIL
    return c >= 0.0


def filter_auto_candidates(
    rows: list[dict[str, Any]],
    *,
    held: set[str],
    watchlist: list[str],
    cooldown: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Узкий список для автостола: ядро + рукав альта + watch + позиции."""
    cool = {str(t).upper() for t in (cooldown or set())}
    held_u = {str(t).upper() for t in held}
    watch_u = [str(t).upper() for t in watchlist]
    allow = DESK_CORE | DESK_ALT_SLEEVE | held_u | set(watch_u)
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
            # Mean-reversion: новые альты не берём уже на дневном разгоне.
            if ticker in DESK_ALT_SLEEVE and chg > ALT_ENTRY_MAX_DAY_PCT:
                continue
        seen.add(ticker)
        out.append(row)
    return out


def _row_score(row: dict[str, Any]) -> float:
    """Скор ядра: лёгкий импульс + бонус стабильности."""
    chg7 = row.get("chg_7")
    chg = float(row.get("chg") or 0)
    score = 0.0
    if chg7 is not None:
        score += float(chg7) * 1.5
    score += chg * 0.5
    ticker = str(row.get("ticker") or "").upper()
    if ticker in DESK_CORE:
        score += 2.0
    return score


def _alt_row_score(row: dict[str, Any]) -> float:
    """Скор альта: mean-reversion — выше при дневной/недельной просадке, не на пампе."""
    chg = float(row.get("chg") or 0)
    chg7 = row.get("chg_7")
    if chg <= -15.0:
        dip = 0.0  # обвал — не ловим нож
    elif chg < 0:
        dip = -chg
    else:
        dip = -chg * 0.8  # растущие штрафуем
    score = dip * 1.5
    if chg7 is not None:
        c7 = float(chg7)
        if c7 < 0:
            score += min(-c7, 12.0) * 0.4
        else:
            score -= c7 * 0.35
    return score


def _pick_bucket(
    candidates: list[dict[str, Any]],
    *,
    budget_pct: float,
    max_names: int,
    allow_weak_core: bool,
    score_fn=None,
) -> dict[str, float]:
    """Доли внутри бюджета. Пустой — бюджет остаётся кэшем."""
    score_fn = score_fn or _row_score
    budget = max(0.0, float(budget_pct))
    if budget <= 0 or not candidates:
        return {}
    ranked = sorted(
        (
            row
            for row in candidates
            if str(row.get("ticker") or "").upper()
            and _chg7_allowed(str(row.get("ticker") or ""), row.get("chg_7"))
        ),
        key=score_fn,
        reverse=True,
    )
    picked = ranked[: max(1, int(max_names))]
    usable: list[dict[str, Any]] = []
    for row in picked:
        score = score_fn(row)
        ticker = str(row.get("ticker") or "").upper()
        chg7 = row.get("chg_7")
        if score > 0:
            usable.append(row)
            continue
        if allow_weak_core and ticker in DESK_CORE and _chg7_allowed(ticker, chg7):
            usable.append(row)
    if not usable:
        return {}
    weights = [max(score_fn(row), 0.5) for row in usable]
    total_w = sum(weights) or 1.0
    alloc = {
        str(row["ticker"]).upper(): round(budget * (w / total_w), 1)
        for row, w in zip(usable, weights)
    }
    s = sum(alloc.values())
    if s > 0 and abs(s - budget) > 0.2:
        alloc = {k: round(v * budget / s, 1) for k, v in alloc.items()}
    return alloc


def score_alloc(
    candidates: list[dict[str, Any]],
    *,
    max_names: int = DESK_MAX_NAMES,
    cash_floor_pct: float = DESK_CASH_FLOOR_PCT,
    alt_sleeve_pct: float = ALT_SLEEVE_MAX_PCT,
    alt_max_names: int = ALT_MAX_NAMES,
) -> tuple[dict[str, float], str]:
    """Ядро + рукав альта (MR). Недобор рукава → кэш. Сумма ≤ 100 − cash_floor."""
    floor = max(0.0, min(100.0, float(cash_floor_pct)))
    if floor >= 100.0:
        return {}, "Рынок слабый — держу кэш в тетере."
    sleeve = max(0.0, min(float(alt_sleeve_pct), 100.0 - floor))
    core_budget = max(0.0, 100.0 - floor - sleeve)

    core_rows = [
        row for row in candidates
        if str(row.get("ticker") or "").upper() in DESK_CORE
    ]
    alt_rows = [
        row for row in candidates
        if str(row.get("ticker") or "").upper() in DESK_ALT_SLEEVE
    ]

    core_alloc = _pick_bucket(
        core_rows,
        budget_pct=core_budget,
        max_names=max_names,
        allow_weak_core=True,
        score_fn=_row_score,
    )
    alt_alloc = _pick_bucket(
        alt_rows,
        budget_pct=sleeve,
        max_names=alt_max_names,
        allow_weak_core=False,
        score_fn=_alt_row_score,
    )
    if not core_alloc and not alt_alloc:
        return {}, "Рынок слабый — держу кэш в тетере."

    alloc = {**core_alloc, **alt_alloc}
    parts: list[str] = []
    if core_alloc:
        parts.append(
            "ядро "
            + ", ".join(f"{k} {int(round(v))}%" for k, v in core_alloc.items())
        )
    if alt_alloc:
        parts.append(
            "альты "
            + ", ".join(f"{k} {int(round(v))}%" for k, v in alt_alloc.items())
        )
    else:
        parts.append(f"альты 0% (≤{int(sleeve)}% → кэш)")
    body = "; ".join(parts)
    return alloc, f"Скор-стол: {body}, кэш ≥ {int(floor)}%."


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
    take_profit_pct: float | None = None,
) -> bool:
    """Коридор: не трогаем ногу, пока отклонение меньше max(band% equity, min_trade).

    Take-profit: сильный дневной памп при избытке над целью — фиксируем даже внутри band.
    """
    excess = float(current_value) - float(target_value)
    tp = TAKE_PROFIT_DAY_PCT if take_profit_pct is None else float(take_profit_pct)
    if (
        day_chg is not None
        and float(day_chg) >= tp
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
    dip_pct: float | None = None,
) -> bool:
    """Докупка ядра и альта к сохранённой цели на дневной просадке."""
    t = str(ticker or "").upper()
    if t in DESK_CORE:
        threshold = WATCH_DIP_DAY_PCT if dip_pct is None else float(dip_pct)
    elif t in DESK_ALT_SLEEVE:
        threshold = WATCH_DIP_ALT_DAY_PCT if dip_pct is None else float(dip_pct)
    else:
        return False
    gap = float(target_value) - float(current_value)
    if gap < float(min_trade_usd):
        return False
    if day_chg is None:
        return False
    return float(day_chg) <= float(threshold)


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
