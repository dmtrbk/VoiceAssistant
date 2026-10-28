# skills/crypto/desk_policy.py
# Правила автостола: режим рынка, скоринг, коридор, фильтры. Без сети и Groq.
# Каркас этики: robotics_laws.py — приказы хозяина (настройки/.env), без вреда человеку, беречь стол.

from __future__ import annotations

import time
from typing import Any

# Рукав альтов: до 50% счёта, 2 слота. Карман SOL 25%. Остаток — кэш и щупы.
# Берём только на провале у нижней полосы.
DESK_ALT_SLEEVE = frozenset({
    "BTC", "ETH",
    "SOL", "XRP", "DOGE", "LINK", "AVAX", "SUI", "AVA", "NEAR", "ADA", "MNT", "BNB", "APT", "DOT",
})
ALT_SLEEVE_MAX_PCT = 50.0
ALT_MAX_NAMES = 2
PROBE_MAX_LEGS = 3
DESK_CASH_FLOOR_PCT = 25.0
DESK_CASH_FLOOR_BULL_PCT = 8.0
REBALANCE_BAND_PCT = 6.0
REBALANCE_BAND_ALT_PCT = 8.0
# Исключение по неделе: вход при 7д ≤ −5%, выход при 7д ≥ −1%.
# Между этими порогами список не прыгает.
EXCLUDE_ENTER_CHG7 = -5.0
EXCLUDE_EXIT_CHG7 = -1.0
BTC_RISK_OFF_7D = -5.0
BTC_RISK_OFF_DAY = -3.0
BTC_BULL_7D = 5.0
# Альты: Боллинджер 4h (MA20 ± 2σ), равные слоты рукава. Пороги подобраны crypto_backtest.py
# (333 дня часовых свечей Bybit; рыночные заявки — taker 0.18% за сторону, проверено и на 0.1%).
ALT_BB_INTERVAL = "240"
ALT_BB_ENTRY_Z = -2.5
# 0,4% ловил шум в начале падения. Нужен заметный отскок, и дно не должно обновляться.
ALT_ENTRY_BOUNCE_PCT = 1.5
ALT_ENTRY_HOLD_SEC = 5 * 60
ALT_ENTRY_STILL_Z = -1.2
ALT_ENTRY_CANCEL_Z = -0.5
POCKET_ENTRY_BOUNCE_PCT = 1.0
POCKET_ENTRY_HOLD_SEC = 10 * 60
# Карман SOL и AVA: откат от верха последних 4 часов, не только сутки −3%.
POCKET_SWING_PCT = 3.0
POCKET_SWING_BARS = 16
# Живой выход рукава — ловушка вершины, не касание средней.
# Взвод: рост покрыл taker×2 + $0.50. Продажа на откате −0,5% после 2 минут, плюс ещё ≥ порога взвода.
# Прогон выше +8% не режем: это верх рабочей зоны, не потолок.
ALT_EXIT_ARM_PCT = 3.0  # устаревший %; живой взвод — exit_trap_arm_pct()
ALT_EXIT_PULLBACK_PCT = 0.5
ALT_EXIT_HOLD_SEC = 2 * 60
ALT_EXIT_MIN_GAIN_PCT = 1.0  # старый порог у средней; его держит crypto_backtest.py
ALT_STOP_LOSS_PCT = 4.0
# Выше этого % от дна — догон, сетап рукава сбрасываем (как CAP +20% пока ждали свечу).
ALT_CHASE_PCT = 5.0
ALT_HELD_MIN_USD = 5.0  # меньше — пыль, не позиция
MIN_TURNOVER_USD = 5_000_000.0
# Суточный потолок +25% остался только у impulse: уже разогнанную свечу не догоняем.
# Ловушка: круг комиссий (taker 0,18% × 2) + полдоллара сверх.
EXIT_FEE_SIDE = 0.0018
EXIT_TRAP_EXTRA_USD = 0.50
EXIT_TRAP_DEFAULT_COST_USD = 100.0

# Стейблы не рукав, даже если оборот большой. Базовый список остаётся всегда.
SLEEVE_SKIP = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "USDS", "USD1", "RLUSD",
    "TUSD", "USDD", "PYUSD", "USDY", "USDP", "GUSD", "EURC", "EUR", "USD",
})
_live_extra: set[str] = set()
# Щуп: 30$ на пробой тихой полки. Не слот рукава и не карман.
PROBE_USD = 30.0
PROBE_MIN_TURNOVER_USD = 1_000_000.0
PROBE_DAY_MIN_PCT = 1.0
PROBE_NEAR_HIGH = 0.99
PROBE_BOUNCE_MIN_PCT = 8.0
PROBE_BOUNCE_NEAR_HIGH = 0.92
PROBE_SHELF_MAX_RANGE_PCT = 12.0
PROBE_BREAK_MIN_PCT = 4.0
PROBE_BREAK_MAX_PCT = 12.0
PROBE_VOL_MULT = 3.0
PROBE_LOCAL_BARS = 8
PROBE_LOCAL_VOL_MULT = 2.0
PROBE_LOCAL_DRIFT_PCT = 2.5
PROBE_ENTRY_HOLD_SEC = 90
PROBE_CHASE_PCT = 4.0
PROBE_SHELF_DRIFT_PCT = 1.5
PROBE_SHELF_DRIFT_BARS = 8
PROBE_RUN_PCT = 6.0
PROBE_MAX_LOSS_PCT = 8.0
PROBE_SCAN_LIMIT = 10
PROBE_PERIOD_SEC = 60
PROBE_ARMED_PERIOD_SEC = 15
PROBE_EMA_FAST = 20
PROBE_EMA_SLOW = 50
PROBE_SHELF_BARS = 96
# Несколько реакций щупа: импульс → лестница у хая → спринт по полке → классика 4 мин.
PROBE_ALGO_IMPULSE = "impulse"
PROBE_ALGO_GRIND = "grind"
PROBE_ALGO_SPRINT = "sprint"
PROBE_ALGO_SHELF = "shelf"
PROBE_ALGO_ORDER = (
    PROBE_ALGO_IMPULSE,
    PROBE_ALGO_GRIND,
    PROBE_ALGO_SPRINT,
    PROBE_ALGO_SHELF,
)
PROBE_SPRINT_HOLD_SEC = 30
PROBE_SPRINT_CHASE_PCT = 5.0
PROBE_IMPULSE_HOLD_SEC = 12
PROBE_IMPULSE_CHASE_PCT = 7.0
PROBE_IMPULSE_LOOKBACK = 12
PROBE_IMPULSE_VOL_MULT = 2.5
PROBE_IMPULSE_BREAK_MIN_PCT = 3.0
PROBE_IMPULSE_BREAK_MAX_PCT = 15.0
PROBE_IMPULSE_MAX_DAY_PCT = 25.0
# Grind без дневного потолка ловит уже разогнанные тикеры у хая суток.
PROBE_GRIND_MAX_DAY_PCT = 20.0
# Лестница: ближе к началу хода, не только у самого хая. Свежий пробой +4% — не grind.
PROBE_GRIND_BARS = 16
PROBE_GRIND_NEAR_HIGH = 0.97
PROBE_GRIND_MIN_RISE_PCT = 3.0
# Окно 4ч уже уехало сильнее — это не свежая лестница, а догон хая.
PROBE_GRIND_MAX_RISE_PCT = 15.0
PROBE_GRIND_MAX_EXT_PCT = 6.0
PROBE_GRIND_MAX_BAR_PCT = 8.0
PROBE_GRIND_FLOOR_BARS = 4
PROBE_GRIND_HOLD_SEC = 15
PROBE_GRIND_CHASE_PCT = 5.0
# Уже держанное: при сильном дневном пампе фиксируем избыток даже внутри коридора.
TAKE_PROFIT_DAY_PCT = 18.0
TAKE_PROFIT_ALT_DAY_PCT = 12.0  # альты раньше фиксируем на подъёме
# Дозор (~12 мин): выход альтов и новые входы у нижней полосы.
WATCH_TP_DAY_PCT = 12.0
WATCH_TP_ALT_DAY_PCT = 8.0
# Карман SOL: 25% счёта на дневной просадке.
# Уже открытый карман не добираем: новый размер — со следующего входа.
BTC_DIP_TICKER = "SOL"
BTC_DIP_PCT = 25.0
BTC_DIP_DAY_PCT = -3.0
# Устаревший фиксированный %; живой взвод кармана — exit_trap_arm_pct().
BTC_DIP_EXIT_PCT = 1.1
# Второй карман: те же сутки и отскок, фиксированные 50$ вместо доли счёта.
SUI_DIP_TICKER = "AVA"
SUI_DIP_USD = 50.0
# Плюс (комиссия×2 + $0.50) взводит вершину. Продажа — откат от неё, пауза короче входа.
POCKET_EXIT_PULLBACK_PCT = 0.5
POCKET_EXIT_HOLD_SEC = 5 * 60
BTC_DIP_STOP_PCT = 15.0
WATCH_MAX_TRADES_PER_HOUR = 2
WATCH_PERIOD_CALM = 12 * 60
WATCH_PERIOD_NEAR = 2 * 60
WATCH_PERIOD_HOT = 60
NEAR_TP_PCT = 0.35
NEAR_SL_PCT = 3.0
NEAR_MA_PCT = 0.4
TRAIL_ARM_PCT = 5.0
TRAIL_ALT_PCT = 8.0
# После offset — туже, как trailing_stop_positive у Freqtrade.
TRAIL_POSITIVE_PCT = 4.0
CHURN_COOLDOWN_HOURS = 12.0
# Свежекупленный альт не ротируем полным столом (трейл, TP и risk-off продают как обычно).
ALT_MIN_HOLD_HOURS = 12.0


def cfg():
    """Живые пороги из настроек стола. Нет файла — рабочие числа выше."""
    from skills.crypto.settings import current

    return current()


def update_exclusions(
    excluded: set[str],
    rows: list[dict[str, Any]],
) -> set[str]:
    """Монеты с неделей ≤ −5% в исключениях. Неделя ≥ −1% — выход из списка.

    Значение между порогами состояние не меняет.
    """
    current = {str(ticker).upper() for ticker in excluded}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in sleeve_names():
            continue
        chg7 = row.get("chg_7")
        if chg7 is None:
            continue
        week = float(chg7)
        if ticker not in current and week <= EXCLUDE_ENTER_CHG7:
            current.add(ticker)
        elif ticker in current and week >= EXCLUDE_EXIT_CHG7:
            current.discard(ticker)
    return current


def btc_dip_should_buy(*, day_chg: float | None, qty: float) -> bool:
    """Покупка кармана, только если его ещё нет и сутки просели."""
    if qty > 0 or day_chg is None:
        return False
    return float(day_chg) <= cfg().pocket_day_pct


def exit_trap_arm_pct(
    *,
    entry: float = 0.0,
    qty: float = 0.0,
    cost_usd: float = 0.0,
) -> float:
    """Порог взвода ловушки в %: taker×2 на ногу + $0.50 сверх.

    Нет размера — считаем от типичных $100, чтобы дозор не молчал.
    """
    cost = float(cost_usd or 0)
    if cost <= 0:
        ent = float(entry or 0)
        q = float(qty or 0)
        if ent > 0 and q > 0:
            cost = ent * q
    if cost <= 0:
        cost = EXIT_TRAP_DEFAULT_COST_USD
    need = cost * (2.0 * EXIT_FEE_SIDE) + EXIT_TRAP_EXTRA_USD
    return need / cost * 100.0


def btc_dip_exit_reason(
    *,
    price: float,
    entry: float,
    exit_arm: dict[str, Any] | None = None,
    now: float | None = None,
    swing_low: float = 0.0,
) -> str | None:
    """Стоп −15% на дыре. Обычный срыв — возврат под локальное дно. Плюс закрываем откатом от вершины."""
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        return None
    gain = (px / ent - 1.0) * 100.0
    if gain <= -cfg().pocket_stop_pct:
        return "стоп"
    if float(swing_low or 0) > 0 and px <= float(swing_low):
        return "дно"
    if pocket_exit_ready(exit_arm, price=px, now=now):
        return "вершина"
    return None


def _leveraged_base(ticker: str) -> bool:
    """Плечо Bybit: BTC3L, ETHDOWN. Короткое имя вроде PUMP не плечо."""
    name = str(ticker or "").upper()
    if name.endswith(("3L", "3S", "5L", "5S", "DOWN")):
        return True
    return name.endswith("UP") and len(name) > 4


def sleeve_names() -> frozenset[str]:
    """Базовый рукав плюс монеты, которые стол добавил по обороту."""
    return DESK_ALT_SLEEVE | frozenset(_live_extra)


def set_live_sleeve(tickers: set[str] | frozenset[str]) -> None:
    """Запомнить оборотный рукав в этом процессе. Базовый список не снимается."""
    global _live_extra
    _live_extra = {
        str(ticker).upper()
        for ticker in tickers
        if str(ticker).upper() and str(ticker).upper() not in DESK_ALT_SLEEVE
    }


def liquid_sleeve_names(rows: list[dict[str, Any]]) -> set[str]:
    """Базовый список и любой спот с оборотом ≥ $5 млн. Стейблы и плечо мимо."""
    names = set(DESK_ALT_SLEEVE)
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if (
            not ticker
            or ticker in SLEEVE_SKIP
            or _leveraged_base(ticker)
        ):
            continue
        try:
            turnover = float(row.get("turnover") or 0)
        except (TypeError, ValueError):
            continue
        if turnover >= MIN_TURNOVER_USD:
            names.add(ticker)
    return names


def is_alt_sleeve(ticker: str) -> bool:
    return str(ticker or "").upper() in sleeve_names()


def take_profit_day_pct_for(ticker: str) -> float:
    return TAKE_PROFIT_ALT_DAY_PCT if is_alt_sleeve(ticker) else TAKE_PROFIT_DAY_PCT


def watch_tp_day_pct_for(ticker: str) -> float:
    return WATCH_TP_ALT_DAY_PCT if is_alt_sleeve(ticker) else WATCH_TP_DAY_PCT


def btc_dip_quote(equity: float) -> float:
    """Сколько USDT класть в карман SOL при входе: доля счёта."""
    return max(0.0, float(equity or 0)) * cfg().pocket_pct / 100.0


def sui_dip_quote() -> float:
    """Второй карман: фиксированная сумма, не доля счёта."""
    return float(cfg().sui_usd)


def second_pocket_choice() -> str:
    """SUI или AVA. Чужое значение остаётся AVA."""
    raw = str(cfg().second_ticker or "AVA").upper()
    return raw if raw in {"SUI", "AVA"} else "AVA"


def probe_scan_rank(row: dict[str, Any]) -> float:
    """Кого смотреть: зелёный день или отскок от суточного дна, ещё не уехавший от хая."""
    try:
        chg = float(row.get("chg") or 0)
        price = float(row.get("price") or 0)
        low = float(row.get("low") or 0)
        high = float(row.get("high") or 0)
        turn = float(row.get("turnover") or 0)
    except (TypeError, ValueError):
        return -1.0
    # Сутки +25% щуп пока не отсекает: лестница часто уже там.
    if turn < PROBE_MIN_TURNOVER_USD or price <= 0:
        return -1.0
    if chg >= PROBE_DAY_MIN_PCT:
        return chg
    bounce = (price / low - 1.0) * 100.0 if low > 0 else 0.0
    if bounce >= PROBE_BOUNCE_MIN_PCT and high > 0 and price >= high * PROBE_BOUNCE_NEAR_HIGH:
        return bounce
    return -1.0


def probe_quote() -> float:
    """Щуп: фиксированные 30$, не доля счёта."""
    return float(cfg().probe_usd)


def probe_reserve_quote(*, open_legs: int, pending: int = 0) -> float:
    """Пустой щуп резервирует один слот. Взвод или открытая нога держат место и под второй."""
    if not cfg().probe_on:
        return 0.0
    opened = max(0, int(open_legs))
    waiting = max(0, int(pending))
    want = PROBE_MAX_LEGS if opened + waiting >= 1 else 1
    return probe_quote() * max(0, want - opened)


def ema_last(values: list[float], period: int) -> float | None:
    """Последнее значение EMA. Мало точек — сигнала нет."""
    period = int(period)
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = float(price) * k + ema * (1.0 - k)
    return ema


def ma_trend(closes: list[float]) -> str:
    """Бычий/медвежий тренд как сводка средних: быстрая EMA против медленной и цены.

    Совпадает по смыслу со вкладкой «Торговый анализ» Bybit, без осцилляторов.
    """
    if len(closes) < PROBE_EMA_SLOW:
        return "neutral"
    fast = ema_last(closes, PROBE_EMA_FAST)
    slow = ema_last(closes, PROBE_EMA_SLOW)
    price = float(closes[-1])
    if fast is None or slow is None or price <= 0:
        return "neutral"
    if fast > slow and price > fast:
        return "bull"
    if fast < slow and price < fast:
        return "bear"
    return "neutral"


def _median(values: list[float]) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _probe_window_setup(
    bars: list[dict[str, Any]],
    *,
    shelf_bars: int,
    vol_mult: float,
    min_len: int,
    drift_pct: float = 0.0,
) -> dict[str, float] | None:
    """Одна полка: тихий коридор и живой пробой с объёмом."""
    forming = bars[-1]
    closed = bars[:-1]
    if len(closed) < min_len + 1:
        return None
    shelf = closed[:-1][-int(shelf_bars):]
    if len(shelf) < min_len:
        return None
    try:
        high = max(float(bar["high"]) for bar in shelf)
        low = min(float(bar["low"]) for bar in shelf)
        vols = [float(bar["volume"]) for bar in shelf if float(bar["volume"]) > 0]
        price = float(forming["close"])
        impulse = max(float(forming.get("volume") or 0), float(closed[-1].get("volume") or 0))
    except (TypeError, ValueError, KeyError):
        return None
    if low <= 0 or high <= 0 or price <= 0 or not vols:
        return None
    limits = cfg()
    if (high - low) / low * 100.0 > limits.probe_shelf_range_pct:
        return None
    floor = high * (1.0 + limits.probe_break_min_pct / 100.0)
    ceiling = high * (1.0 + limits.probe_break_max_pct / 100.0)
    if price < floor or price > ceiling:
        return None
    typical = _median(vols)
    if typical <= 0 or impulse < typical * float(vol_mult):
        return None
    prev_high = float(closed[-1].get("high") or 0)
    if prev_high >= high * (1.0 + limits.probe_break_min_pct / 100.0):
        return None
    if drift_pct > 0:
        tail = shelf[-min(PROBE_SHELF_DRIFT_BARS, len(shelf)):]
        if len(tail) >= 2:
            old = float(tail[0].get("close") or 0)
            new = float(tail[-1].get("close") or 0)
            if old > 0 and abs(new / old - 1.0) * 100.0 > drift_pct:
                return None
    quiet = [float(bar.get("volume") or 0) for bar in shelf[:-2] if float(bar.get("volume") or 0) > 0]
    base = _median(quiet) if quiet else typical
    if base > 0:
        for bar in shelf[-2:]:
            if float(bar.get("volume") or 0) >= base * float(vol_mult):
                return None
    return {"shelf": high, "price": price}


def probe_shelf_setup_daily(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Суточная тихая полка + объёмный пробой."""
    if len(bars) < PROBE_EMA_SLOW + 2:
        return None
    return _probe_window_setup(
        bars,
        shelf_bars=PROBE_SHELF_BARS,
        vol_mult=PROBE_VOL_MULT,
        min_len=20,
        drift_pct=PROBE_SHELF_DRIFT_PCT,
    )


def probe_shelf_setup_local(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Локальная полка ~2ч — быстрее суточной, для спринта."""
    if len(bars) < PROBE_LOCAL_BARS + 2:
        return None
    return _probe_window_setup(
        bars,
        shelf_bars=PROBE_LOCAL_BARS,
        vol_mult=PROBE_LOCAL_VOL_MULT,
        min_len=PROBE_LOCAL_BARS,
        drift_pct=PROBE_LOCAL_DRIFT_PCT,
    )


def probe_shelf_setup(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Пробой узкой полки: сутки 3× или локальные 2 часа 2×.

    bars — от старых к новым, последняя свеча может быть ещё открытой.
    Полка — до двух последних 15-минутных свечей.
    """
    return probe_shelf_setup_daily(bars) or probe_shelf_setup_local(bars)


def probe_impulse_setup(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """5м подножка: объёмный пробой локального хая без тихой суточной полки.

    bars — от старых к новым, последняя может быть открытой.
    """
    need = PROBE_IMPULSE_LOOKBACK + 3
    if len(bars) < need:
        return None
    forming = bars[-1]
    closed = bars[:-1]
    if len(closed) < PROBE_IMPULSE_LOOKBACK + 1:
        return None
    window = closed[-(PROBE_IMPULSE_LOOKBACK + 1):-1]
    breakout = closed[-1]
    try:
        prior_high = max(float(bar["high"]) for bar in window)
        vols = [float(bar["volume"]) for bar in window if float(bar.get("volume") or 0) > 0]
        impulse_vol = max(float(breakout.get("volume") or 0), float(forming.get("volume") or 0))
        price = float(forming.get("close") or 0)
        break_close = float(breakout.get("close") or 0)
        break_high = float(breakout.get("high") or 0)
    except (TypeError, ValueError, KeyError):
        return None
    if prior_high <= 0 or price <= 0 or not vols:
        return None
    typical = _median(vols)
    if typical <= 0 or impulse_vol < typical * PROBE_IMPULSE_VOL_MULT:
        return None
    floor = prior_high * (1.0 + PROBE_IMPULSE_BREAK_MIN_PCT / 100.0)
    ceiling = prior_high * (1.0 + PROBE_IMPULSE_BREAK_MAX_PCT / 100.0)
    # Пробой уже на закрытой 5м или на живой свече.
    tip = max(break_close, break_high, price)
    if tip < floor or price > ceiling:
        return None
    if price < prior_high:
        return None
    return {"shelf": prior_high, "price": price}


def probe_grind_setup(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Лестница у хая: подъём без свежего пробоя полки.

    bars — 15м, от старых к новым, последняя может быть открытой.
    Полка здесь — локальный пол последних свечей, не хай: под ним вход снимаем.
    Свежий пробой +break_min оставляем sprint/shelf.
    """
    if len(bars) < PROBE_GRIND_BARS + 1:
        return None
    forming = bars[-1]
    closed = bars[:-1]
    if len(closed) < PROBE_GRIND_BARS:
        return None
    window = closed[-PROBE_GRIND_BARS:]
    try:
        high = max(float(bar["high"]) for bar in window)
        low = min(float(bar["low"]) for bar in window)
        price = float(forming.get("close") or 0)
        spans = []
        for bar in window:
            lo = float(bar["low"])
            hi = float(bar["high"])
            if lo <= 0:
                return None
            spans.append((hi - lo) / lo * 100.0)
        older = window[:-2] if len(window) > 4 else window
        prior = max(float(bar["high"]) for bar in older)
        floor_src = window[-PROBE_GRIND_FLOOR_BARS:]
        shelf = min(float(bar["low"]) for bar in floor_src)
    except (TypeError, ValueError, KeyError):
        return None
    if high <= 0 or low <= 0 or price <= 0 or prior <= 0 or shelf <= 0:
        return None
    rise = (price / low - 1.0) * 100.0
    if rise < PROBE_GRIND_MIN_RISE_PCT or rise > PROBE_GRIND_MAX_RISE_PCT:
        return None
    if price < high * PROBE_GRIND_NEAR_HIGH:
        return None
    if price > high * (1.0 + PROBE_GRIND_MAX_EXT_PCT / 100.0):
        return None
    if max(spans) > PROBE_GRIND_MAX_BAR_PCT:
        return None
    if price >= prior * (1.0 + cfg().probe_break_min_pct / 100.0):
        return None
    if price <= shelf:
        return None
    return {"shelf": shelf, "price": price}


def probe_algo_params(algo: str) -> dict[str, float]:
    """hold_sec / chase_pct для алгоритма щупа."""
    name = str(algo or PROBE_ALGO_SHELF).strip().lower() or PROBE_ALGO_SHELF
    if name == PROBE_ALGO_IMPULSE:
        return {
            "hold_sec": float(PROBE_IMPULSE_HOLD_SEC),
            "chase_pct": float(PROBE_IMPULSE_CHASE_PCT),
        }
    if name == PROBE_ALGO_SPRINT:
        return {
            "hold_sec": float(PROBE_SPRINT_HOLD_SEC),
            "chase_pct": float(PROBE_SPRINT_CHASE_PCT),
        }
    if name == PROBE_ALGO_GRIND:
        return {
            "hold_sec": float(PROBE_GRIND_HOLD_SEC),
            "chase_pct": float(PROBE_GRIND_CHASE_PCT),
        }
    return {
        "hold_sec": float(PROBE_ENTRY_HOLD_SEC),
        "chase_pct": float(PROBE_CHASE_PCT),
    }


def probe_pick_setup(
    bars15: list[dict[str, Any]],
    bars5: list[dict[str, Any]] | None = None,
    *,
    order: tuple[str, ...] | list[str] | None = None,
    day_chg: float | None = None,
) -> dict[str, Any] | None:
    """Первый сработавший: impulse → grind → sprint → shelf.

    Impulse не берём, если сутки уже +25% и выше: это покупка у пика дня.
    Grind — с +20%: слабее импульс, но тоже поздно заходить в дневной разгон.
    """
    seq = tuple(order or PROBE_ALGO_ORDER)
    for algo in seq:
        name = str(algo or "").strip().lower()
        if (
            name == PROBE_ALGO_IMPULSE
            and day_chg is not None
            and float(day_chg) >= PROBE_IMPULSE_MAX_DAY_PCT
        ):
            continue
        if (
            name == PROBE_ALGO_GRIND
            and day_chg is not None
            and float(day_chg) >= PROBE_GRIND_MAX_DAY_PCT
        ):
            continue
        if name == PROBE_ALGO_IMPULSE:
            setup = probe_impulse_setup(bars5 or [])
        elif name == PROBE_ALGO_GRIND:
            setup = probe_grind_setup(bars15)
        elif name == PROBE_ALGO_SPRINT:
            setup = probe_shelf_setup_local(bars15)
        elif name == PROBE_ALGO_SHELF:
            setup = probe_shelf_setup_daily(bars15)
        else:
            continue
        if not setup:
            continue
        params = probe_algo_params(name)
        return {**setup, "algo": name, **params}
    return None


def probe_trends_ok(
    trend15: str,
    trend1h: str,
    day_chg: float | None,
    week_chg: float | None = None,
    *,
    algo: str = "",
) -> bool:
    """15м бычий, час не медвежий, неделя не слабее −5%.

    Grind требует бычий час: нейтральный 1h часто = уже выдохшийся подъём.
    """
    if trend15 != "bull" or trend1h == "bear":
        return False
    if str(algo or "").strip().lower() == PROBE_ALGO_GRIND and trend1h != "bull":
        return False
    if day_chg is None:
        return False
    if week_chg is not None and float(week_chg) <= EXCLUDE_ENTER_CHG7:
        return False
    return True


def probe_arm_action(
    *,
    armed_ts: float,
    now: float,
    price: float,
    shelf: float,
    trend15: str,
    day_chg: float | None,
    arm_px: float = 0.0,
    week_chg: float | None = None,
    algo: str = "",
    hold_sec: float | None = None,
    chase_pct: float | None = None,
) -> str:
    """buy — держится над полкой; drop — пробой погас или цена ушла; wait — ещё рано."""
    if shelf <= 0 or price <= shelf or trend15 != "bull":
        return "drop"
    if (
        str(algo or "").strip().lower() == PROBE_ALGO_IMPULSE
        and day_chg is not None
        and float(day_chg) >= PROBE_IMPULSE_MAX_DAY_PCT
    ):
        return "drop"
    if week_chg is not None and float(week_chg) <= EXCLUDE_ENTER_CHG7:
        return "drop"
    params = probe_algo_params(algo)
    chase = float(params["chase_pct"] if chase_pct is None else chase_pct)
    hold = float(params["hold_sec"] if hold_sec is None else hold_sec)
    if arm_px > 0 and price > arm_px * (1.0 + chase / 100.0):
        return "drop"
    if float(now) - float(armed_ts) < hold:
        return "wait"
    return "buy"


def probe_exit_reason(
    *,
    price: float,
    entry: float,
    shelf: float,
    trend15: str,
    arm_px: float = 0.0,
    peak: float = 0.0,
    entry_low: float = 0.0,
    algo: str = "",
    qty: float = 0.0,
) -> str | None:
    """Стоп и полка. Плюс — ловушка: пик ≥ комиссия×2+$0.50, продажа на откате −0,5%.

    Пока ловушка не взведена: shelf режется «ожиданием»/«дном»; подножка
    impulse/sprint/grind эти срезы не берёт. Большой ход (+PROBE_RUN) ещё и медвежьим 15м.
    """
    px = float(price or 0)
    ent = float(entry or 0)
    level = float(shelf or 0)
    if px <= 0 or ent <= 0:
        return None
    gain = (px / ent - 1.0) * 100.0
    if gain <= -cfg().probe_max_loss_pct:
        return "стоп"
    if level > 0 and px <= level:
        return "полка"
    high = max(float(peak or 0), px)
    arm_pct = exit_trap_arm_pct(entry=ent, qty=qty)
    peak_gain = (high / ent - 1.0) * 100.0 if ent > 0 else 0.0
    if (
        peak_gain >= arm_pct - 1e-6
        and px <= high * (1.0 - cfg().pocket_exit_pullback_pct / 100.0) + 1e-12
        and gain >= arm_pct - 1e-6
    ):
        return "вершина"
    run = high >= ent * (1.0 + PROBE_RUN_PCT / 100.0)
    name = str(algo or "").strip().lower()
    footboard = name in {PROBE_ALGO_IMPULSE, PROBE_ALGO_SPRINT, PROBE_ALGO_GRIND}
    if not run and not footboard:
        if float(entry_low or 0) > 0 and px <= float(entry_low):
            return "дно"
        scratch = float(arm_px or 0) or ent
        if scratch > 0 and px <= scratch:
            return "ожидание"
    if run and trend15 == "bear":
        return "тренд"
    if not run and gain <= 0 and trend15 != "bull":
        return "тренд"
    return None


def pocket_cash_reserve(equity: float, *, pocket_open: bool) -> float:
    """Пока кармана нет, эти деньги не отдаём рукаву: они ждут просадку SOL."""
    if pocket_open:
        return 0.0
    return btc_dip_quote(equity)


def alt_slot_pct(sleeve_pct: float | None = None, max_names: int | None = None) -> float:
    limits = cfg()
    if not limits.sleeve_on and sleeve_pct is None:
        return 0.0
    sleeve = limits.sleeve_max_pct if sleeve_pct is None else float(sleeve_pct)
    names = limits.sleeve_max_names if max_names is None else int(max_names)
    return round(max(0.0, sleeve) / max(1, names), 1)


def alt_entry_ok(bb_z: Any) -> bool:
    """Новый альт / докуп: цена у нижней полосы 4h."""
    if bb_z is None:
        return False
    return float(bb_z) <= cfg().sleeve_entry_z


def _arm_low(state: dict[str, Any] | None, price: float, now: float) -> dict[str, float]:
    """Новое дно сбрасывает таймер: отскок считается только после паузы."""
    px = float(price)
    if state is None or px < float(state.get("low") or 0) - 1e-12:
        return {"low": px, "ts": float(now)}
    return {"low": float(state["low"]), "ts": float(state.get("ts") or now)}


def _bounce_held(state: dict[str, Any], now: float | None, hold_sec: float) -> bool:
    if now is None:
        return True
    return float(now) - float(state.get("ts") or 0) >= float(hold_sec)


def alt_chase_too_far(state: dict[str, Any] | None, price: float) -> bool:
    """Цена уже далеко от дна — покупать поздно, сетап сбрасываем."""
    if not state:
        return False
    low = float(state.get("low") or 0)
    px = float(price or 0)
    if low <= 0 or px <= 0:
        return False
    return px >= low * (1.0 + cfg().sleeve_chase_pct / 100.0) - 1e-12


def update_alt_arm(
    state: dict[str, Any] | None,
    *,
    price: float,
    z: float | None,
    now: float,
) -> dict[str, float] | None:
    """Сигнал z ≤ −2,5 запоминает дно. Близко к средней или chase — сигнал снимается."""
    if z is None:
        return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
    zf = float(z)
    limits = cfg()
    if state is not None and zf > limits.sleeve_cancel_z:
        return None
    px = float(price or 0)
    if px <= 0:
        return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
    if state is not None and alt_chase_too_far(state, px):
        return None
    if state is None:
        if zf <= limits.sleeve_entry_z:
            return {"low": px, "ts": float(now)}
        return None
    return _arm_low(state, px, now)


def alt_bounce_ready(
    state: dict[str, Any] | None,
    *,
    price: float,
    z: float | None,
    now: float | None = None,
) -> bool:
    """Покупка рукава: +1,5% от дна, дно держится 5 минут, без догона выше chase, z ещё не у средней."""
    limits = cfg()
    if not state or z is None or float(z) > limits.sleeve_still_z:
        return False
    if not _bounce_held(state, now, limits.sleeve_hold_sec):
        return False
    low = float(state.get("low") or 0)
    px = float(price or 0)
    if low <= 0 or px <= 0:
        return False
    if alt_chase_too_far(state, px):
        return False
    return px >= low * (1.0 + limits.sleeve_bounce_pct / 100.0) - 1e-12


def update_pocket_arm(
    state: dict[str, Any] | None,
    *,
    price: float,
    day_chg: float | None,
    now: float,
) -> dict[str, float] | None:
    """Сутки ≤ −3% запоминают дно. Выше порога сигнал снимается."""
    if day_chg is None:
        return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
    if float(day_chg) > cfg().pocket_day_pct:
        return None
    px = float(price or 0)
    if px <= 0:
        return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
    if state is None:
        return {"low": px, "ts": float(now)}
    return _arm_low(state, px, now)


def pocket_bounce_ready(
    state: dict[str, Any] | None,
    *,
    price: float,
    now: float | None = None,
) -> bool:
    """Покупка кармана: +1% от дна, и это дно уже 10 минут не обновлялось."""
    limits = cfg()
    if not state or not _bounce_held(state, now, limits.pocket_hold_sec):
        return False
    low = float(state.get("low") or 0)
    px = float(price or 0)
    if low <= 0 or px <= 0:
        return False
    return px >= low * (1.0 + limits.pocket_bounce_pct / 100.0) - 1e-12


# Лента видит минимум чуть ниже low свечи Bybit. 0,5% — та же свеча, не другой откат.
POCKET_DIP_LOW_SLACK_PCT = 0.5


def _dip_candle(bar_low: float, floor: float) -> bool:
    """Свеча напечатала это дно, с допуском на тик ленты ниже kline."""
    if bar_low <= 0 or floor <= 0:
        return False
    return bar_low <= floor * (1.0 + POCKET_DIP_LOW_SLACK_PCT / 100.0) + 1e-12


def _pocket_dip_index(bars: list[dict[str, Any]], low: float) -> int | None:
    """Индекс свечи дна среди закрытых. None — дна в закрытых нет или открытая его обновляет."""
    if len(bars) < 3 or float(low or 0) <= 0:
        return None
    floor = float(low)
    if float(bars[-1].get("low") or 0) <= floor:
        return None
    closed = bars[:-1]
    for i in range(len(closed) - 1, -1, -1):
        if _dip_candle(float(closed[i].get("low") or 0), floor):
            return i
    return None


def pocket_dip_pending(bars: list[dict[str, Any]], low: float) -> bool:
    """Дно в последней закрытой свече, следующая ещё не закрылась. Взвод не снимать."""
    idx = _pocket_dip_index(bars, low)
    if idx is None:
        return False
    return idx == len(bars) - 2


def pocket_dip_held(bars: list[dict[str, Any]], low: float) -> bool:
    """Дно уже в закрытой свече, и следующая 15м закрылась, не обновив его.

    bars — от старых к новым, последняя свеча может быть открытой.
    Тик ленты чуть ниже low свечи считается тем же дном.
    """
    if len(bars) < 3 or float(low or 0) <= 0:
        return False
    try:
        floor = float(low)
        idx = _pocket_dip_index(bars, low)
        closed = bars[:-1]
        # Подтверждение — именно последняя закрытая свеча. Часовой давности откат уже отыгран.
        if idx is None or idx + 1 != len(closed) - 1:
            return False
        return float(closed[idx + 1].get("low") or 0) > floor
    except (TypeError, ValueError):
        return False


def pocket_swing(bars: list[dict[str, Any]], *, pullback_pct: float = POCKET_SWING_PCT) -> float | None:
    """Дно отката, если цена хотя бы на pullback_pct ниже верха последних 4 часов.

    bars — от старых к новым, последняя свеча может быть открытой.
    """
    if len(bars) < 4:
        return None
    closed = bars[:-1]
    window = closed[-POCKET_SWING_BARS:]
    if len(window) < 4:
        return None
    try:
        high = max(float(bar["high"]) for bar in window)
        peak_at = max(range(len(window)), key=lambda i: float(window[i]["high"]))
        tail = list(window[peak_at:]) + [bars[-1]]
        low = min(float(bar["low"]) for bar in tail)
        price = float(bars[-1]["close"])
    except (TypeError, ValueError, KeyError):
        return None
    if high <= 0 or low <= 0 or price <= 0:
        return None
    if (high - price) / high * 100.0 < float(pullback_pct):
        return None
    return low


def update_pocket_swing_arm(
    state: dict[str, Any] | None,
    *,
    price: float,
    swing_low: float | None,
    now: float,
    keep: bool = False,
) -> dict[str, float] | None:
    """Откат ≥3% запоминает дно. Отскок +1% ещё покупаем, даже если откат уже меньше 3%.

    Сигнал снимается, когда цена ушла выше входа больше чем на 1 п.п.:
    окно отскока пропущено. Пока ждём закрытие следующей свечи — не снимаем.
    """
    px = float(price or 0)
    if swing_low is not None and float(swing_low) > 0:
        if px <= 0:
            return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
        seeded = state
        if seeded is None or float(swing_low) < float(seeded.get("low") or 0) - 1e-12:
            seeded = {"low": float(swing_low), "ts": float(now)}
        return _arm_low(seeded, px, now)
    if state is None or px <= 0:
        return None if state is None else {"low": float(state["low"]), "ts": float(state.get("ts") or now)}
    ceiling = float(state["low"]) * (1.0 + (cfg().pocket_bounce_pct + 1.0) / 100.0)
    if px > ceiling and not keep:
        return None
    return {"low": float(state["low"]), "ts": float(state.get("ts") or now)}


def update_pocket_exit_arm(
    state: dict[str, Any] | None,
    *,
    price: float,
    entry: float,
    now: float,
    qty: float = 0.0,
) -> dict[str, float] | None:
    """Плюс ≥ комиссия×2+$0.50 запоминает вершину. Новая вершина сбрасывает таймер.

    Ниже порога ловушка ещё не взведена. Уже взведенная не снимается:
    откат как раз и есть сигнал продажи.
    """
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        if state is None:
            return None
        return {"high": float(state["high"]), "ts": float(state.get("ts") or now)}
    gain = (px / ent - 1.0) * 100.0
    if state is None:
        if gain < exit_trap_arm_pct(entry=ent, qty=qty) - 1e-6:
            return None
        return {"high": px, "ts": float(now)}
    high = float(state.get("high") or 0)
    if px > high + 1e-12:
        return {"high": px, "ts": float(now)}
    return {"high": high, "ts": float(state.get("ts") or now)}


def pocket_exit_ready(
    state: dict[str, Any] | None,
    *,
    price: float,
    now: float | None = None,
) -> bool:
    """Продажа кармана: −0,5% от вершины, и эта вершина уже 5 минут не обновлялась."""
    limits = cfg()
    if not state or not _bounce_held(state, now, limits.pocket_exit_hold_sec):
        return False
    high = float(state.get("high") or 0)
    px = float(price or 0)
    if high <= 0 or px <= 0:
        return False
    return px <= high * (1.0 - limits.pocket_exit_pullback_pct / 100.0) + 1e-12


def position_basis(entry: float, anchor: float | None) -> float:
    """Стоп и ловушка считаются от якоря, если он задан. Иначе от цены покупки."""
    anc = float(anchor or 0)
    if anc > 0:
        return anc
    return float(entry or 0)


def update_alt_exit_arm(
    state: dict[str, Any] | None,
    *,
    price: float,
    entry: float,
    now: float,
    qty: float = 0.0,
) -> dict[str, float] | None:
    """Плюс ≥ комиссия×2+$0.50 запоминает вершину рукава. Новая вершина сбрасывает таймер.

    Ниже порога ловушка ещё не взведена. Уже взведенная не снимается.
    """
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        if state is None:
            return None
        return {"high": float(state["high"]), "ts": float(state.get("ts") or now)}
    gain = (px / ent - 1.0) * 100.0
    if state is None:
        if gain < exit_trap_arm_pct(entry=ent, qty=qty) - 1e-6:
            return None
        return {"high": px, "ts": float(now)}
    high = float(state.get("high") or 0)
    if px > high + 1e-12:
        return {"high": px, "ts": float(now)}
    return {"high": high, "ts": float(state.get("ts") or now)}


def alt_exit_ready(
    state: dict[str, Any] | None,
    *,
    price: float,
    entry: float,
    now: float | None = None,
    qty: float = 0.0,
) -> bool:
    """Продажа рукава: −0,5% от вершины, вершина 2 минуты не обновлялась, плюс ещё ≥ комиссия+$0.50."""
    limits = cfg()
    if not state or not _bounce_held(state, now, limits.sleeve_exit_hold_sec):
        return False
    high = float(state.get("high") or 0)
    px = float(price or 0)
    ent = float(entry or 0)
    if high <= 0 or px <= 0 or ent <= 0:
        return False
    if px > high * (1.0 - limits.sleeve_exit_pullback_pct / 100.0) + 1e-12:
        return False
    gain = (px / ent - 1.0) * 100.0
    return gain >= exit_trap_arm_pct(entry=ent, qty=qty) - 1e-6


def alt_exit_reason(
    *,
    price: float,
    entry: float,
    exit_arm: dict[str, Any] | None = None,
    now: float | None = None,
    ma: float | None = None,
    swing_low: float = 0.0,
    qty: float = 0.0,
) -> str | None:
    """Выход альта: стоп на дыре, обычный срыв — возврат под дно входа, плюс — откат от вершины."""
    del ma
    px = float(price or 0)
    ent = float(entry or 0)
    if px <= 0 or ent <= 0:
        return None
    gain = (px / ent - 1.0) * 100.0
    # Ровно −stop% в float чуть больше порога — иначе стоп молчит.
    if gain <= -cfg().sleeve_stop_pct + 1e-6:
        return "стоп"
    if float(swing_low or 0) > 0 and px <= float(swing_low):
        return "дно"
    if alt_exit_ready(exit_arm, price=px, entry=ent, now=now, qty=qty):
        return "вершина"
    return None


def alt_sleeve_qty_for_exit(
    *,
    ticker: str,
    held_qty: float,
    pocket_qty: float,
    price: float,
    min_trade_usd: float,
) -> float:
    """Объём для альтового выхода: без кармана и без щупа, и без пыли ниже min_trade."""
    del ticker
    qty = max(0.0, float(held_qty or 0) - max(0.0, float(pocket_qty or 0)))
    px = float(price or 0)
    if px <= 0 or qty <= 0:
        return 0.0
    if qty * px + 1e-9 < float(min_trade_usd):
        return 0.0
    return qty


def _held_value(row: dict[str, Any]) -> float:
    if row.get("held_value") is not None:
        return float(row.get("held_value") or 0)
    return float(row.get("held") or 0) * float(row.get("price") or 0)



def is_risk_off(*, btc_chg_7: float | None, btc_chg_day: float | None) -> bool:
    """Слабый BTC: новые альты не берём, уже купленные держим до своего выхода."""
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
    return cfg().pocket_pct


def band_pct_for(ticker: str) -> float:
    """Коридор ребаланса рукава."""
    del ticker
    return cfg().rebalance_pct


def filter_auto_candidates(
    rows: list[dict[str, Any]],
    *,
    held: set[str],
    watchlist: list[str],
    cooldown: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Узкий список для автостола: рукав альта + watch + позиции."""
    cool = {str(t).upper() for t in (cooldown or set())}
    held_u = {str(t).upper() for t in held}
    watch_u = [str(t).upper() for t in watchlist]
    allow = sleeve_names() | held_u | set(watch_u)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in seen or ticker not in allow:
            continue
        if ticker in cool and ticker not in held_u:
            continue
        turnover = float(row.get("turnover") or 0)
        # Уже держанное не режем по обороту — иначе не сможем довести ребаланс.
        if ticker not in held_u:
            if turnover > 0 and turnover < MIN_TURNOVER_USD:
                continue
        seen.add(ticker)
        out.append(row)
    return out


def pick_alts(
    rows: list[dict[str, Any]],
    *,
    sleeve_pct: float | None = None,
    max_names: int | None = None,
    excluded: set[str] | None = None,
    blacklist: set[str] | None = None,
) -> dict[str, float]:
    """Рукав: держанные до выхода + новые у нижней полосы, кроме исключений.

    Чёрный список не даёт новой покупки, но живая нога занимает слот. Исключение по неделе держанное не трогает.
    """
    limits = cfg()
    sleeve_pct = limits.sleeve_max_pct if sleeve_pct is None else float(sleeve_pct)
    max_names = limits.sleeve_max_names if max_names is None else int(max_names)
    slot = alt_slot_pct(sleeve_pct, max_names)
    if slot <= 0:
        return {}
    blocked = {str(t).upper() for t in (excluded or set())}
    banned = {str(t).upper() for t in (blacklist or set())}
    alts = [r for r in rows if str(r.get("ticker") or "").upper() in sleeve_names()]
    held_all = [
        r for r in alts
        if _held_value(r) >= ALT_HELD_MIN_USD
    ]
    banned_held = [r for r in held_all if str(r["ticker"]).upper() in banned]
    held = sorted(
        (r for r in held_all if str(r["ticker"]).upper() not in banned),
        key=_held_value,
        reverse=True,
    )
    held_t = {str(r["ticker"]).upper() for r in held_all}
    fresh = sorted(
        (
            r for r in alts
            if str(r["ticker"]).upper() not in held_t
            and str(r["ticker"]).upper() not in blocked
            and str(r["ticker"]).upper() not in banned
            and alt_entry_ok(r.get("bb_z"))
        ),
        key=lambda r: float(r["bb_z"]),
    )
    room = max(0, int(max_names) - len(banned_held))
    picked = (held + fresh)[:room]
    return {str(r["ticker"]).upper(): slot for r in picked}


def score_alloc(
    candidates: list[dict[str, Any]],
    *,
    cash_floor_pct: float | None = None,
    alt_sleeve_pct: float | None = None,
    alt_max_names: int | None = None,
    excluded: set[str] | None = None,
    blacklist: set[str] | None = None,
) -> tuple[dict[str, float], str]:
    """Рукав альтов (Боллинджер 4h). Недобор и исключения остаются кэшем."""
    limits = cfg()
    if cash_floor_pct is None:
        cash_floor_pct = limits.pocket_pct
    if not limits.sleeve_on and alt_sleeve_pct is None:
        return {}, "Рукав выключен."
    if alt_sleeve_pct is None:
        alt_sleeve_pct = limits.sleeve_max_pct
    if alt_max_names is None:
        alt_max_names = limits.sleeve_max_names
    floor = max(0.0, min(100.0, float(cash_floor_pct)))
    if floor >= 100.0:
        return {}, "Рынок слабый — держу кэш."
    sleeve = max(0.0, min(float(alt_sleeve_pct), 100.0 - floor))
    alt_rows = [
        row for row in candidates
        if str(row.get("ticker") or "").upper() in sleeve_names()
    ]
    banned = {str(t).upper() for t in (blacklist or set())}
    alt_alloc = pick_alts(
        alt_rows,
        sleeve_pct=sleeve,
        max_names=alt_max_names,
        excluded=excluded,
        blacklist=banned,
    )
    if not alt_alloc:
        return {}, "Держу кэш: нет альтов у нижней полосы."
    body = "альты " + ", ".join(f"{k} {int(round(v))}%" for k, v in alt_alloc.items())
    blocked = {str(t).upper() for t in (excluded or set())}
    if blocked:
        body += "; исключения " + ", ".join(sorted(blocked))
    if banned:
        body += "; чёрный список " + ", ".join(sorted(banned))
    return alt_alloc, f"Скор-стол: {body}, кэш ≥ {int(floor)}%."


def drift_usd(current: float, target: float) -> float:
    return abs(current - target)


def should_rebalance_leg(
    *,
    current_value: float,
    target_value: float,
    equity: float,
    band_pct: float | None = None,
    min_trade_usd: float,
    day_chg: float | None = None,
    take_profit_pct: float | None = None,
) -> bool:
    """Коридор: не трогаем ногу, пока отклонение меньше max(band% equity, min_trade).

    Take-profit: сильный дневной памп при избытке над целью — фиксируем даже внутри band.
    """
    excess = float(current_value) - float(target_value)
    if band_pct is None:
        band_pct = cfg().rebalance_pct
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


def watch_stream_tickers(
    *,
    held_values: dict[str, float],
    pocket_open: bool,
    min_usd: float = ALT_HELD_MIN_USD,
) -> set[str]:
    """Держанные ноги рукава выше пыли + SOL, если открыт карман."""
    out = {
        str(ticker).upper()
        for ticker, value in (held_values or {}).items()
        if str(ticker).upper() in sleeve_names() and float(value or 0) >= float(min_usd)
    }
    if pocket_open:
        out.add(BTC_DIP_TICKER)
    return out


def watch_rate_ok(
    watch_trades: list[float] | None,
    *,
    now: float | None = None,
    max_per_hour: int = WATCH_MAX_TRADES_PER_HOUR,
) -> bool:
    """Не больше max_per_hour сделок дозора за последний час. Только входы, не стопы."""
    current = time.time() if now is None else float(now)
    cutoff = current - 3600.0
    recent = [float(ts) for ts in (watch_trades or []) if float(ts) >= cutoff]
    return len(recent) < max(0, int(max_per_hour))


def watch_period_sec(
    *,
    legs: list[dict[str, Any]],
    calm: int = WATCH_PERIOD_CALM,
    near: int = WATCH_PERIOD_NEAR,
    hot: int = WATCH_PERIOD_HOT,
) -> int:
    """12 мин спокойно; 60 с у стопа или когда рукав/карман ведёт вершину."""
    period = int(calm)
    for leg in legs:
        px = float(leg.get("price") or 0)
        ent = float(leg.get("entry") or 0)
        if px <= 0 or ent <= 0:
            continue
        gain = (px / ent - 1.0) * 100.0
        kind = str(leg.get("kind") or "alt")
        limits = cfg()
        arm = exit_trap_arm_pct(entry=ent, qty=float(leg.get("qty") or 0))
        if kind == "pocket":
            if float(leg.get("exit_high") or 0) > 0:
                return int(hot)
            if gain >= arm - NEAR_TP_PCT:
                return int(hot)
            if gain <= -limits.pocket_stop_pct + NEAR_SL_PCT:
                return int(hot)
            continue
        if float(leg.get("exit_high") or 0) > 0:
            return int(hot)
        if gain >= arm - NEAR_TP_PCT:
            return int(hot)
        if gain <= -limits.sleeve_stop_pct + NEAR_SL_PCT:
            return int(hot)
    return period


def watch_snapshot_line(
    ticker: str,
    *,
    price: float,
    entry: float,
    ma: float | None = None,
    kind: str = "alt",
    exit_high: float = 0.0,
    basis: float = 0.0,
    qty: float = 0.0,
) -> str:
    """Одна нога дозора: avg, цена, дистанция до стопа/тейка."""
    name = str(ticker or "?").upper()
    px = float(price or 0)
    ent = float(entry or 0)
    rule = position_basis(ent, basis)
    if px <= 0 or ent <= 0:
        return f"{name} px={px:.4g} avg=?"
    book = (px / ent - 1.0) * 100.0
    gain = (px / rule - 1.0) * 100.0 if rule > 0 else book
    arm = exit_trap_arm_pct(entry=rule or ent, qty=qty)
    bits = [f"{name} px={px:.4g} avg={ent:.4g} {book:+.2f}%"]
    if rule > 0 and abs(rule - ent) / ent > 1e-6:
        bits.append(f"отсчёт {rule:.4g} {gain:+.2f}%")
    if kind == "pocket":
        hi = float(exit_high or 0)
        stop = cfg().pocket_stop_pct
        if hi > 0:
            need = hi * (1.0 - cfg().pocket_exit_pullback_pct / 100.0)
            bits.append(f"верх {hi:.4g} откат до {need:.4g} до стопа {gain + stop:.1f}пп")
        else:
            bits.append(f"до ловушки {arm - gain:+.2f}пп до стопа {gain + stop:.1f}пп")
    else:
        hi = float(exit_high or 0)
        limits = cfg()
        if hi > 0:
            pull = hi * (1.0 - limits.sleeve_exit_pullback_pct / 100.0)
            floor = rule * (1.0 + arm / 100.0)
            need = pull if pull >= floor else floor
            bits.append(f"верх {hi:.4g} откат до {need:.4g} до стопа {gain + limits.sleeve_stop_pct:.1f}пп")
        else:
            bits.append(
                f"до ловушки {arm - gain:+.2f}пп до стопа {gain + limits.sleeve_stop_pct:.1f}пп"
            )
        if ma and float(ma) > 0:
            bits.append(f"MA {px / float(ma) - 1.0:+.2%}")
    return " ".join(bits)


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
    excluded: set[str] | None = None,
) -> bool:
    """Новый альт у нижней полосы. Держанный не усредняем, исключённый не берём."""
    del dip_pct
    t = str(ticker or "").upper()
    if t not in sleeve_names() or t in {str(x).upper() for x in (excluded or set())}:
        return False
    del day_chg
    gap = float(target_value) - float(current_value)
    if gap < float(min_trade_usd):
        return False
    return float(current_value) < ALT_HELD_MIN_USD and alt_entry_ok(bb_z)


def pick_watch_alt_entries(
    bands: dict[str, dict[str, float] | None],
    *,
    target_alloc: dict[str, float],
    held_alts: set[str],
    blocked: set[str],
    max_names: int | None = None,
    excluded: set[str] | None = None,
) -> list[str]:
    """Дозор: новые альты у нижней полосы 4h, пока в рукаве есть свободные слоты."""
    if max_names is None:
        max_names = cfg().sleeve_max_names
    banned = {str(t).upper() for t in (excluded or set())}
    taken = {t for t in target_alloc if t in sleeve_names()} | set(held_alts)
    room = max(0, int(max_names) - len(taken))
    if room <= 0:
        return []
    ready = [
        (float(b["z"]), t)
        for t, b in bands.items()
        if t in sleeve_names()
        and t not in taken
        and t not in blocked
        and t not in banned
        and b
        and alt_entry_ok(b.get("z"))
    ]
    ready.sort()
    return [t for _z, t in ready[:room]]


def trail_pct_for(ticker: str) -> float | None:
    """Дистанция трейла дозора (% от пика). None — ногу не ведём."""
    del ticker
    return TRAIL_ALT_PCT


def calculate_trailing_stop(
    *,
    price: float,
    entry: float,
    peak: float | None = None,
    prev_stop: float | None = None,
    stoploss_pct: float = 10.0,
    trail_pct: float | None = None,
    positive_trail_pct: float | None = None,
    offset_pct: float = 0.0,
    only_offset: bool = False,
) -> dict[str, Any]:
    """Трейлинг-стоп в духе Freqtrade `ft_stoploss_adjust` / Trailing Stop Loss.

    Отличия от Freqtrade (явно):
    - У Freqtrade `stoploss` / `trailing_stop_positive` — доли (−0.10, 0.02).
      У нас проценты стола: 10.0 = −10%, 2.0 = −2% от пика (как sleeve_stop_pct).
    - Только long spot: нет `is_short`, нет leverage.
    - Нет объекта Trade и `adjust_stop_loss`: возвращаем снимок для JSON/дозора.
    - Стоп для long только поднимается (не ослабевает), как у Freqtrade.
    - `only_offset` ≈ `trailing_only_offset_is_reached`: до offset стоп статичен
      от входа; после — тянется от пика.
    - `positive_trail_pct` ≈ `trailing_stop_positive`: более тугой трейл после offset.
    - `hit` считаем по текущей цене (live); в бэктесте Freqtrade сверяет high/low свечи.

    Пример (Freqtrade docs → наши %): entry=100, stoploss_pct=10, trail как 10%,
    peak=102 → stop=91.8; при positive_trail_pct=2 и offset_pct=3, peak=103.5 → stop=101.43.
    """
    px = float(price or 0)
    ent = float(entry or 0)
    if ent <= 0 and px > 0:
        ent = px
    if px <= 0 or ent <= 0:
        return {
            "entry": max(ent, 0.0),
            "peak": max(float(peak or 0), ent, 0.0),
            "stop": float(prev_stop or 0),
            "trail_pct": float(stoploss_pct or 0),
            "gain_pct": 0.0,
            "trailing": False,
            "hit": False,
        }

    # Freqtrade: bound = high для long в бэктесте; в live — current_rate.
    # У нас peak = max(предыдущий high, entry, текущая цена).
    hi = max(float(peak or 0), ent, px)
    # gain от пика для решения «какой trail %», как bound_profit у Freqtrade.
    peak_gain_pct = (hi / ent - 1.0) * 100.0
    gain_pct = (px / ent - 1.0) * 100.0

    base_trail = float(stoploss_pct if trail_pct is None else trail_pct)
    base_trail = max(0.0, base_trail)
    pos_trail = None if positive_trail_pct is None else max(0.0, float(positive_trail_pct))
    offset = max(0.0, float(offset_pct or 0))

    # Статический стоп от входа: Freqtrade initial adjust_stop_loss(open_rate, stoploss).
    initial_stop = ent * (1.0 - base_trail / 100.0)

    # trailing_only_offset_is_reached: до offset не двигаем стоп от пика.
    can_trail = (not only_offset) or (peak_gain_pct + 1e-12 >= offset)

    # Freqtrade: если profit > offset и задан trailing_stop_positive — берём его как дистанцию.
    active_trail = base_trail
    if (
        can_trail
        and pos_trail is not None
        and peak_gain_pct + 1e-12 >= offset
    ):
        active_trail = pos_trail

    if can_trail:
        # stop = peak * (1 - trail%): аналог adjust_stop_loss(bound, stop_loss_value).
        trailed = hi * (1.0 - active_trail / 100.0)
        # Стоп только ужесточаем (для long — только вверх).
        stop = max(initial_stop, trailed, float(prev_stop or 0))
        trailing = True
    else:
        stop = max(initial_stop, float(prev_stop or 0))
        trailing = False

    hit = px <= stop + 1e-12
    return {
        "entry": ent,
        "peak": hi,
        "stop": stop,
        "trail_pct": active_trail if trailing else base_trail,
        "gain_pct": gain_pct,
        "trailing": trailing,
        "hit": hit,
    }


def update_trail_leg(
    *,
    price: float,
    entry: float,
    high: float | None = None,
    armed: bool = False,
    arm_pct: float = TRAIL_ARM_PCT,
    trail_pct: float = 6.0,
    prev_stop: float | None = None,
    positive_trail_pct: float | None = None,
) -> dict[str, Any]:
    """Обновить водяную марку. hit=True — цена пробила подтягивающийся стоп.

    Обёртка над calculate_trailing_stop: arm_pct → only_offset / offset_pct.
    До взвода hit не ставим (старый контракт дозора), даже если цена ниже
    начального stoploss от входа.
    """
    out = calculate_trailing_stop(
        price=price,
        entry=entry,
        peak=high,
        prev_stop=prev_stop,
        stoploss_pct=float(trail_pct),
        trail_pct=float(trail_pct),
        positive_trail_pct=positive_trail_pct,
        offset_pct=float(arm_pct),
        only_offset=True,
    )
    is_armed = bool(armed) or bool(out["trailing"])
    return {
        "entry": out["entry"],
        "high": out["peak"],
        "armed": is_armed,
        "stop": out["stop"],
        "hit": bool(out["hit"]) and is_armed,
        "gain_pct": out["gain_pct"],
        "trailing": out["trailing"],
        "trail_pct": out["trail_pct"],
    }
