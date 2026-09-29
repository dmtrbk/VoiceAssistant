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
    "XRP", "DOGE", "LINK", "AVAX", "SUI", "NEAR", "ADA", "MNT", "BNB", "APT", "DOT",
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
# Альты: Боллинджер 4h (MA20 ± 2σ), равные слоты рукава. Пороги подобраны crypto_backtest.py
# (333 дня часовых свечей Bybit; рыночные заявки — taker 0.18% за сторону, проверено и на 0.1%).
ALT_BB_INTERVAL = "240"
ALT_BB_ENTRY_Z = -2.5
ALT_EXIT_MIN_GAIN_PCT = 1.0  # выход у средней, только если прибыль перекрывает 2×комиссию с запасом
ALT_STOP_LOSS_PCT = 15.0
ALT_HELD_MIN_USD = 5.0  # меньше — пыль, не позиция
MIN_TURNOVER_USD = 5_000_000.0
MAX_DAY_PUMP_PCT = 25.0
# Уже держанное: при сильном дневном пампе фиксируем избыток даже внутри коридора.
TAKE_PROFIT_DAY_PCT = 18.0
TAKE_PROFIT_ALT_DAY_PCT = 12.0  # альты раньше фиксируем на подъёме
# Дозор (~12 мин): TP / докуп на просадке (ядро + рукав альта).
WATCH_TP_DAY_PCT = 12.0
WATCH_TP_ALT_DAY_PCT = 8.0
WATCH_DIP_DAY_PCT = -5.0
WATCH_DIP_BUY_FRAC = 0.5
WATCH_MAX_TRADES_PER_HOUR = 2
# Трейлинг-стоп дозора: защита пика с покупки (только позиции стола).
TRAIL_ARM_PCT = 5.0
TRAIL_CORE_PCT = 6.0
TRAIL_ALT_PCT = 8.0
CHURN_COOLDOWN_HOURS = 12.0
# Свежекупленный альт не ротируем полным столом (трейл, TP и risk-off продают как обычно).
ALT_MIN_HOLD_HOURS = 12.0


def is_alt_sleeve(ticker: str) -> bool:
    return str(ticker or "").upper() in DESK_ALT_SLEEVE


def take_profit_day_pct_for(ticker: str) -> float:
    return TAKE_PROFIT_ALT_DAY_PCT if is_alt_sleeve(ticker) else TAKE_PROFIT_DAY_PCT


def watch_tp_day_pct_for(ticker: str) -> float:
    return WATCH_TP_ALT_DAY_PCT if is_alt_sleeve(ticker) else WATCH_TP_DAY_PCT


def alt_slot_pct(sleeve_pct: float = ALT_SLEEVE_MAX_PCT, max_names: int = ALT_MAX_NAMES) -> float:
    return round(max(0.0, float(sleeve_pct)) / max(1, int(max_names)), 1)


def alt_entry_ok(bb_z: Any) -> bool:
    """Новый альт / докуп: цена у нижней полосы 4h."""
    if bb_z is None:
        return False
    return float(bb_z) <= ALT_BB_ENTRY_Z


def alt_exit_reason(*, price: float, entry: float, ma: float | None) -> str | None:
    """Выход альта целиком: стоп от входа или возврат к средней 4h с прибылью."""
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        return None
    gain = (px / ent - 1.0) * 100.0
    if gain <= -ALT_STOP_LOSS_PCT:
        return "стоп"
    if ma is not None and px >= float(ma) and gain >= ALT_EXIT_MIN_GAIN_PCT:
        return "средняя"
    return None


def _held_value(row: dict[str, Any]) -> float:
    if row.get("held_value") is not None:
        return float(row.get("held_value") or 0)
    return float(row.get("held") or 0) * float(row.get("price") or 0)



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
    """Уже коридор для ядра, шире для альтов — меньше шума."""
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


def pick_alts(
    rows: list[dict[str, Any]],
    *,
    sleeve_pct: float = ALT_SLEEVE_MAX_PCT,
    max_names: int = ALT_MAX_NAMES,
) -> dict[str, float]:
    """Рукав альтов: держанные до выхода + новые у нижней полосы 4h, по равному слоту."""
    slot = alt_slot_pct(sleeve_pct, max_names)
    if slot <= 0:
        return {}
    alts = [r for r in rows if str(r.get("ticker") or "").upper() in DESK_ALT_SLEEVE]
    held = sorted(
        (r for r in alts if _held_value(r) >= ALT_HELD_MIN_USD),
        key=_held_value,
        reverse=True,
    )
    held_t = {str(r["ticker"]).upper() for r in held}
    fresh = sorted(
        (
            r for r in alts
            if str(r["ticker"]).upper() not in held_t and alt_entry_ok(r.get("bb_z"))
        ),
        key=lambda r: float(r["bb_z"]),
    )
    picked = (held + fresh)[: max(0, int(max_names))]
    return {str(r["ticker"]).upper(): slot for r in picked}


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
    """Ядро + рукав альта (Боллинджер 4h). Недобор рукава → кэш. Сумма ≤ 100 − cash_floor."""
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
    alt_alloc = pick_alts(alt_rows, sleeve_pct=sleeve, max_names=alt_max_names)
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
    bb_z: float | None = None,
) -> bool:
    """Добор к сохранённой цели: ядро — на дневной просадке, альт — вход у нижней полосы 4h.

    Держанный альт не усредняем: стоп считается от цены входа.
    """
    t = str(ticker or "").upper()
    if t not in DESK_CORE and t not in DESK_ALT_SLEEVE:
        return False
    gap = float(target_value) - float(current_value)
    if gap < float(min_trade_usd):
        return False
    if t in DESK_ALT_SLEEVE:
        return float(current_value) < ALT_HELD_MIN_USD and alt_entry_ok(bb_z)
    if day_chg is None:
        return False
    threshold = WATCH_DIP_DAY_PCT if dip_pct is None else float(dip_pct)
    return float(day_chg) <= float(threshold)


def pick_watch_alt_entries(
    bands: dict[str, dict[str, float] | None],
    *,
    target_alloc: dict[str, float],
    held_alts: set[str],
    blocked: set[str],
    max_names: int = ALT_MAX_NAMES,
) -> list[str]:
    """Дозор: новые альты у нижней полосы 4h, пока в рукаве есть свободные слоты."""
    taken = {t for t in target_alloc if t in DESK_ALT_SLEEVE} | set(held_alts)
    room = max(0, int(max_names) - len(taken))
    if room <= 0:
        return []
    ready = [
        (float(b["z"]), t)
        for t, b in bands.items()
        if t in DESK_ALT_SLEEVE
        and t not in taken
        and t not in blocked
        and b
        and alt_entry_ok(b.get("z"))
    ]
    ready.sort()
    return [t for _z, t in ready[:room]]


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
