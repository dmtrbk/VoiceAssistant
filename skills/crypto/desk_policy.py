# skills/crypto/desk_policy.py
# Правила автостола: режим рынка, скоринг, коридор, фильтры. Без сети и Groq.

from __future__ import annotations

from typing import Any

# Ядро whitelist для авто (плюс уже держанные позиции).
DESK_CORE = frozenset({"BTC", "ETH", "SOL"})
DESK_MAX_NAMES = 3
DESK_CASH_FLOOR_PCT = 25.0
REBALANCE_BAND_PCT = 6.0
BTC_RISK_OFF_7D = -5.0
BTC_RISK_OFF_DAY = -3.0
MIN_TURNOVER_USD = 5_000_000.0
MAX_DAY_PUMP_PCT = 25.0
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
    ranked = sorted(
        (
            row
            for row in candidates
            if str(row.get("ticker") or "").upper()
            and (row.get("chg_7") is None or float(row.get("chg_7") or 0) >= 0)
        ),
        key=_row_score,
        reverse=True,
    )
    picked = ranked[: max(1, int(max_names))]
    # Только положительный скор или ядро с неотрицательным 7д.
    usable: list[dict[str, Any]] = []
    for row in picked:
        score = _row_score(row)
        ticker = str(row.get("ticker") or "").upper()
        chg7 = row.get("chg_7")
        if score <= 0 and not (ticker in DESK_CORE and (chg7 is None or float(chg7) >= 0)):
            continue
        usable.append(row)
    if not usable:
        return {}, "Рынок слабый — держу кэш в тетере."
    invest_pct = max(0.0, 100.0 - float(cash_floor_pct))
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
    return alloc, f"Скор-стол: {names}, кэш ≥ {int(cash_floor_pct)}%."


def drift_usd(current: float, target: float) -> float:
    return abs(current - target)


def should_rebalance_leg(
    *,
    current_value: float,
    target_value: float,
    equity: float,
    band_pct: float = REBALANCE_BAND_PCT,
    min_trade_usd: float,
) -> bool:
    """Коридор: не трогаем ногу, пока отклонение меньше max(band% equity, min_trade)."""
    equity = max(float(equity), 1.0)
    band = max(equity * (band_pct / 100.0), float(min_trade_usd))
    return drift_usd(current_value, target_value) >= band
