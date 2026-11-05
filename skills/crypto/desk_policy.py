# skills/crypto/desk_policy.py
# Правила щупа и кармана. Рукав/alloc — desk_policy_legacy.py.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import time
from typing import Any

_YEKT = timezone(timedelta(hours=5))

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROBE_TUNE_PATH = os.path.join(_PROJECT_DIR, "jarvis_crypto_probe_tune.json")

# Рукав альтов: до 50% счёта, 2 слота. Карман SOL 25%. Остаток — кэш и щупы.
# Берём только на провале у нижней полосы.
DESK_ALT_SLEEVE = frozenset({
    "BTC", "ETH",
    "SOL", "XRP", "DOGE", "LINK", "AVAX", "SUI", "AVA", "NEAR", "ADA", "MNT", "BNB", "APT", "DOT",
})
ALT_SLEEVE_MAX_PCT = 50.0
ALT_MAX_NAMES = 2
PROBE_MAX_LEGS = 4
# BTC за сутки в минусе — не четыре коррелированные полки, а 1–2 слота.
PROBE_MAX_LEGS_BTC_DOWN = 2
# Сколько переданных с щупа ног держит второй карман (отдельно от щупа).
POCKET_FROM_PROBE_MAX = 6
# Мягкий выход щупа → передача во второй карман; жёсткий — продажа.
# «дно» не передаём: цена уже у low щупа — карман2 сразу резал бы тем же дном.
PROBE_HANDOFF_REASONS = frozenset({"ожидание", "тренд", "застой"})
PROBE_SELL_REASONS = frozenset({"стоп", "полка", "вершина", "дно", "срез"})
PROBE_CANDLE_STALE_SEC = 2 * 3600
# Срез только если сломана полка. Минус к входу за 90 с при живой полке — не режем (ICNT).
PROBE_FAIL_SCRATCH_SEC = 90.0
# Парковка в карман2 ради свободного слота: свежую ногу не трогаем.
PROBE_ROOM_MIN_AGE_SEC = 15 * 60
POCKET_SCRATCH_PCT = -0.20
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
# Ловушка кармана/рукава: круг комиссий (taker 0,18% × 2) + $0.12 сверх (на $40 ~+0.66%).
# Щуп: взвод позже (~+1.56% на $40) и откат 0.60% — иначе вершина забирает +$0.05 и не даёт ходу.
EXIT_FEE_SIDE = 0.0018
EXIT_TRAP_EXTRA_USD = 0.12
PROBE_TRAP_EXTRA_USD = 0.48
PROBE_TRAP_PULLBACK_PCT = 0.60
EXIT_TRAP_DEFAULT_COST_USD = 100.0

# Стейблы не рукав, даже если оборот большой. Базовый список остаётся всегда.
SLEEVE_SKIP = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "USDS", "USD1", "RLUSD",
    "TUSD", "USDD", "PYUSD", "USDY", "USDP", "GUSD", "EURC", "EUR", "USD",
})
_live_extra: set[str] = set()
# Щуп: 40$ на пробой тихой полки / свечи. Не слот рукава и не карман.
PROBE_USD = 40.0
# Днём берём монеты с оборотом ≥ $150k (тонкий стакан <$150k на живых сделках дал 1W/11L и -$5.62).
# Ночь 23:00–06:00 YEKT: только stair/flag/impulse/spark, оборот ≥ $250k.
# Вечер 21–23 — как день. Жёсткий режим после убытков — по-прежнему ≥ $1.5M.
PROBE_MIN_TURNOVER_USD = 150_000.0
PROBE_NIGHT_MIN_TURNOVER_USD = 250_000.0
PROBE_STRICT_MIN_TURNOVER_USD = 1_500_000.0
PROBE_NIGHT_START_HOUR_YEKT = 23
PROBE_NIGHT_END_HOUR_YEKT = 6
# Защита от падающего рынка и каскада стопов:
PROBE_BTC_MAX_DROP_DAY_PCT = -2.5
PROBE_MIN_MARKET_BREADTH_PCT = 20.0
PROBE_MARKET_FALL_PAUSE_SEC = 3600.0
PROBE_HOURLY_MAX_LOSS_USD = 1.50
PROBE_MAX_CONSECUTIVE_LOSSES = 3
PROBE_STREAK_PAUSE_SEC = 3600.0
# После среза «полка» не перерабатывать слот в тот же чоп (ZAMA→DOOD, LA→INIT).
PROBE_SHELF_COOLDOWN_SEC = 25 * 60
PROBE_SHELF_COOLDOWN_LOSS_USD = 0.25
# Жёсткий режим (оборот $1.5M, OBI 0.70, только impulse/spark) после убытков.
# На растущем рынке не включаем: резка входов мешает, пока идёт ход.
PROBE_STRICT_MODE = True
PROBE_RISING_BTC_MIN_PCT = 0.0
PROBE_RISING_BREADTH_PCT = 50.0
# Ниже 0.5% сутки 29.10 резали INIT-контекст и оставляли PARTI 18:25 (−$3.28).
# Пол −3% на полном scan: щуп +$1.04, этой сделки нет.
PROBE_DAY_MIN_PCT = -3.0
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
# Сначала candle по широкому списку (только 5м); остальные алго — укороченный топ.
# При floor $25k кандидатов ~120–130 — лимит почти = вся очередь, не топ-48.
PROBE_SCAN_LIMIT = 128
# Impulse по тем же лидерам суток, не по узким 16.
PROBE_SCAN_CLASSIC_LIMIT = 50
# Топ-N по росту за 24ч всегда первыми: свежие 5м и spark, не хвост за ликвидными середняками.
PROBE_SCAN_MANDATORY_TOP = 50
# Чаще сканируем: растущую 5м свечу надо брать в первой половине, не раз в минуту у хая.
PROBE_PERIOD_SEC = 10
PROBE_ARMED_PERIOD_SEC = 6
# После любой продажи щуп не возвращается в ту же монету сразу (KAS: вершина → тот же grind).
# Короткий блок: иначе BLEND слили и 2ч не вернулись в памп.
PROBE_REENTRY_HOURS = 0.75
# Если цена уже выше продажи на этот % — блок снимаем (продолжение хода).
PROBE_REENTRY_BREAKOUT_PCT = 2.5
# Мягкий автопин: 1 слот сначала смотрит лидера суток, не занимает его вхолостую.
PROBE_PIN_ON = True
PROBE_PIN_MIN_TURNOVER_USD = 1_200_000.0
PROBE_PIN_MIN_DAY_PCT = 7.0
PROBE_PIN_MAX_DAY_PCT = 35.0
PROBE_PIN_MIN_ROOM_PCT = 1.0
PROBE_PIN_MAX_ROOM_PCT = 12.0
PROBE_PIN_DROP_ROOM_PCT = 0.4
PROBE_PIN_HOLD_SEC = 45 * 60
PROBE_PIN_MAX_HOLD_SEC = 3 * 3600
PROBE_PIN_TOP_N = 30
PROBE_PIN_KEEP_TOP_N = 8
PROBE_PIN_MIN_SCORE = 45.0
PROBE_PIN_LOSS_LOOKBACK_SEC = 12 * 3600
# Крошечный срез на сильном дне (ICNT −$0.30 при +13%) пин не банит.
PROBE_PIN_SCRATCH_IGNORE_USD = 0.50
PROBE_PIN_SWITCH_SCORE_MULT = 1.15
PROBE_NIGHT_PIN_MAX_LEGS = 2
PROBE_EMA_FAST = 20
PROBE_EMA_SLOW = 50
PROBE_SHELF_BARS = 96
# Только живая свеча (INIT: open→середина бара → ловушка).
# Impulse/sprint/shelf сегодня: CPOOL/HYPE — поздний вход и срез за минуты.
PROBE_ALGO_CANDLE = "candle"
PROBE_ALGO_IMPULSE = "impulse"
PROBE_ALGO_SPARK = "spark"
PROBE_ALGO_GRIND = "grind"
PROBE_ALGO_SPRINT = "sprint"
PROBE_ALGO_SHELF = "shelf"
# Candle: ступенька 2/3 бара, иначе флаг. Выстрел — impulse; старт — spark.
PROBE_ALGO_ORDER = (PROBE_ALGO_CANDLE, PROBE_ALGO_IMPULSE, PROBE_ALGO_SPARK)
# 5м зелёная свеча: ловим начало восходящей свечи от open, без верхнего фитиля (слива).
PROBE_CANDLE_LOOKBACK = 12
# 0,25% ловил шум (CSPR/BICO +0,5%). INIT был +0,67…1,3% от open.
PROBE_CANDLE_MIN_BODY_PCT = 0.6
PROBE_CANDLE_MAX_BODY_PCT = 2.0
# Начало/середина восходящей свечи (pos 0.35..0.65):
# продавленные сверху фитили (PARTI 18:20 pos=0.32, BLEND 13:25 pos=0.20, DOOD pos=0.21)
# и покупка на самом пике бара (pos > 0.65) отсекаются.
PROBE_CANDLE_MIN_BAR_POS = 0.35
PROBE_CANDLE_MAX_BAR_POS = 0.65
# Верхний фитиль ≤ 0.90% от open, отступ от кончика high ≥ 0.25%; нижний пролив под open ≤ 0.80%.
PROBE_CANDLE_MAX_WICK_PCT = 0.90
PROBE_CANDLE_MAX_DIP_PCT = 0.80
PROBE_CANDLE_TIP_GAP_PCT = 0.25
# В первые минуты 5м свечи нормируем порог объёма по доле прошедшего времени (min 0.30 бара).
PROBE_CANDLE_MIN_ELAPSED_FRAC = 0.30
# Касание open — фитиль той же свечи, не провал. Полка — только уход под open.
PROBE_CANDLE_SHELF_BREAK_PCT = 0.4
PROBE_CANDLE_VOL_MULT = 1.5
PROBE_CANDLE_PRIOR_MAX_BODY_PCT = 5.0
# 5с hold = CPOOL вечный tip-drop: хай уезжает, bar_pos убивает buy.
PROBE_CANDLE_HOLD_SEC = 1
PROBE_CANDLE_CHASE_PCT = 1.2
# Запас хода до хая суток ≥ 1% (режет BR/PORTAL под самым пиком, пускает BLUR/BEAM/BIRB/EDGE), без перегрева ≥45%.
PROBE_CANDLE_MAX_DAY_PCT = 45.0
PROBE_CANDLE_MIN_DAY_ROOM_PCT = 1.0
PROBE_CANDLE_MAX_DAY_DROP_PCT = 28.0
# Отсечение перегрева за 30м (>2.2%), кульминации объёма на пред. баре (>6.5x) и верхних фитилей слива (>0.85%).
PROBE_CANDLE_MAX_30M_CHG_PCT = 2.20
PROBE_CANDLE_MAX_PRIOR_VOL_MULT = 6.5
PROBE_CANDLE_MAX_PREV3_WICK_PCT = 0.85
# Ступенька (3 зелёных 5м бара подряд с растущими low и close, как TREE/BIRB/WET/MPLX):
# на 3-м баре цена идёт плотно у верха (pos ≥ 0.70, wick ≤ 0.30%), допускаем room ≥ 0.3%,
# и требуем поджатие к верхней границе часового диапазона (pos_in_1h ≥ 0.85).
PROBE_STAIR_MIN_RISE_PCT = 0.70
PROBE_STAIR_MAX_RISE_PCT = 2.40
PROBE_STAIR_MIN_BODY_PCT = 0.20
PROBE_STAIR_MAX_BODY_PCT = 1.50
PROBE_STAIR_MIN_BAR_POS = 0.70
PROBE_STAIR_MAX_WICK_PCT = 0.30
PROBE_STAIR_MAX_DIP_PCT = 0.35
PROBE_STAIR_VOL2_MULT = 1.5
PROBE_STAIR_VOL_MIN_MULT = 1.1
PROBE_STAIR_MIN_DAY_ROOM_PCT = 0.3
PROBE_STAIR_MIN_1H_POS = 0.85
# Быстрая 2-свечная ступенька (prior зелёная + forming пробивает prior.high с общим ростом 0.80..1.80%):
PROBE_STAIR2_MIN_RISE_PCT = 0.80
PROBE_STAIR2_MAX_RISE_PCT = 1.80
PROBE_STAIR2_MIN_BODY_PCT = 0.25
PROBE_STAIR2_MAX_BODY_PCT = 1.20
PROBE_STAIR2_MIN_BAR_POS = 0.72
PROBE_STAIR2_MAX_WICK_PCT = 0.25
PROBE_STAIR2_MAX_DIP_PCT = 0.30
PROBE_STAIR2_VOL2_MULT = 1.6
PROBE_STAIR2_VOL_MIN_MULT = 1.2
PROBE_STAIR2_MIN_1H_POS = 0.88
# Защита от проскальзывания по пустому стакану перед рыночной покупкой щупа (GROVE 0.007281->0.007409):
PROBE_MAX_SPREAD_PCT = 0.35
PROBE_MAX_ASK_SLIP_PCT = 0.45
# OBI на входе только в жёстком режиме. Днём стакан не режет взвод (ICNT снимали 4 раза).
PROBE_MIN_ENTRY_OBI = 0.55
# Жёсткий режим (бывшие паузы: полка, час, серия, день): щуп не молчит, фильтры туже.
PROBE_STRICT_MIN_ENTRY_OBI = 0.70
PROBE_STRICT_MAX_SPREAD_PCT = 0.20
PROBE_STRICT_MAX_ASK_SLIP_PCT = 0.25
# Жёсткий дневной риск-лимит (Daily Max Drawdown) на новые входы щупа (~1.35% от депозита $440).
PROBE_DAILY_MAX_LOSS_USD = 6.0
PROBE_SPRINT_HOLD_SEC = 30
PROBE_SPRINT_CHASE_PCT = 5.0
PROBE_IMPULSE_HOLD_SEC = 8
PROBE_IMPULSE_CHASE_PCT = 4.0
PROBE_IMPULSE_LOOKBACK = 12
PROBE_IMPULSE_VOL_MULT = 2.5
PROBE_IMPULSE_BREAK_MIN_PCT = 3.0
# Выстрел с базы (NAVX +22%): потолок шире 15%, иначе середина импульса уже «поздно».
PROBE_IMPULSE_BREAK_MAX_PCT = 32.0
PROBE_IMPULSE_MAX_DAY_PCT = 35.0
# Живая свеча выстрела идёт у хая — не требовать середину бара, только не кончик.
PROBE_IMPULSE_MAX_BAR_POS = 0.95
PROBE_IMPULSE_TIP_GAP_PCT = 0.20
# Spark: одиночная 5м в начале бара. Покупка сразу (hold 0): иначе 6–10с до buy и tip.
# Pos до хая бара — ранняя свеча часто закрывается у high.
PROBE_SPARK_MAX_ELAPSED_SEC = 120.0
PROBE_SPARK_MAX_BAR_POS = 1.0
PROBE_SPARK_HOLD_SEC = 0.0
PROBE_SPARK_CHASE_PCT = 1.2
# Grind: горячее candle нельзя; у хая дня (+25%) — уже догон.
PROBE_GRIND_MAX_DAY_PCT = 18.0
# Лестница: ближе к началу хода, не только у самого хая. Свежий пробой +4% — не grind.
PROBE_GRIND_BARS = 16
PROBE_GRIND_NEAR_HIGH = 0.97
PROBE_GRIND_MIN_RISE_PCT = 3.0
# Окно 4ч уже уехало сильнее — это не свежая лестница, а догон хая.
PROBE_GRIND_MAX_RISE_PCT = 10.0
PROBE_GRIND_MAX_EXT_PCT = 4.0
PROBE_GRIND_MAX_BAR_PCT = 8.0
# Живая свеча — не вертикальный выстрел и не покупка у её хая.
PROBE_GRIND_MAX_LIVE_BAR_PCT = 1.5
# Абсолютный откат от high + не в верхней доле диапазона свечи.
PROBE_GRIND_TIP_GAP_PCT = 1.2
PROBE_GRIND_TIP_FRAC = 0.45
PROBE_GRIND_FLOOR_BARS = 4
PROBE_GRIND_HOLD_SEC = 10
# После взвода почти не догоняем — иначе берём пик той же свечи.
PROBE_GRIND_CHASE_PCT = 0.5
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
# Плюс (комиссия×2 + $0.12) взводит вершину. Продажа — откат от неё, пауза короче входа.
POCKET_EXIT_PULLBACK_PCT = 0.35
POCKET_EXIT_WALL_PULLBACK_PCT = 0.20
POCKET_EXIT_DEEP_PULLBACK_PCT = 1.0
POCKET_EXIT_HOLD_SEC = 60
EXIT_TRAP_BEAR_OBI = 0.28
EXIT_TRAP_BULL_OBI = 0.72
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


def read_probe_tune() -> dict[str, Any]:
    try:
        with open(_PROBE_TUNE_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {"tier": 1, "baseline_lifetime": None}


def probe_tune_tier() -> int:
    return max(1, min(4, int(read_probe_tune().get("tier") or 1)))


def probe_period_sec() -> float:
    if probe_tune_tier() >= 2:
        return 6.0
    return float(PROBE_PERIOD_SEC)


def probe_scan_algo_order() -> tuple[str, ...]:
    tier = probe_tune_tier()
    if tier >= 3:
        return (PROBE_ALGO_CANDLE, PROBE_ALGO_IMPULSE, PROBE_ALGO_SPARK)
    return PROBE_ALGO_ORDER


def probe_candle_prior_max_body_pct() -> float:
    if probe_tune_tier() >= 4:
        return 6.0
    return float(PROBE_CANDLE_PRIOR_MAX_BODY_PCT)


def probe_candle_min_body_pct() -> float:
    if probe_tune_tier() >= 4:
        return 0.4
    return float(PROBE_CANDLE_MIN_BODY_PCT)


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
    extra_usd: float | None = None,
) -> float:
    """Порог взвода ловушки в %: taker×2 на ногу + extra сверх.

    Нет размера — считаем от типичных $100, чтобы дозор не молчал.
    extra_usd: карман/рукав $0.12; щуп — PROBE_TRAP_EXTRA_USD.
    """
    cost = float(cost_usd or 0)
    if cost <= 0:
        ent = float(entry or 0)
        q = float(qty or 0)
        if ent > 0 and q > 0:
            cost = ent * q
    if cost <= 0:
        cost = EXIT_TRAP_DEFAULT_COST_USD
    extra = EXIT_TRAP_EXTRA_USD if extra_usd is None else float(extra_usd)
    need = cost * (2.0 * EXIT_FEE_SIDE) + extra
    return need / cost * 100.0


def pick_probe_leg_for_room(
    legs: list[dict[str, Any]],
    prices: dict[str, float],
    *,
    now: float | None = None,
    min_age_sec: float | None = None,
) -> dict[str, Any] | None:
    """Худшая по PnL% нога щупа старше min_age — её паркуем в карман2, слот снова свободен."""
    now_ts = float(now if now is not None else time.time())
    min_age = float(PROBE_ROOM_MIN_AGE_SEC if min_age_sec is None else min_age_sec)
    best: dict[str, Any] | None = None
    best_pnl = float("inf")
    best_age = -1.0
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        ticker = str(leg.get("ticker") or "").upper()
        try:
            qty = float(leg.get("qty") or 0)
            entry = float(leg.get("entry") or 0)
            px = float(prices.get(ticker) or 0)
            opened = float(leg.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not ticker or qty <= 0 or entry <= 0 or px <= 0:
            continue
        age = now_ts - opened if opened > 0 else min_age
        if age < min_age - 1e-12:
            continue
        pnl_pct = (px / entry - 1.0) * 100.0
        if pnl_pct < best_pnl - 1e-12 or (
            abs(pnl_pct - best_pnl) <= 1e-12 and age > best_age
        ):
            best = leg
            best_pnl = pnl_pct
            best_age = age
    return best


def pick_second_pocket_leg_for_funding(
    legs: list[dict[str, Any]],
    prices: dict[str, float],
) -> dict[str, Any] | None:
    """Выбрать ногу кармана2 с наименьшим убытком (или плюсом) для освобождения кэша под щуп."""
    best_leg: dict[str, Any] | None = None
    best_pnl_usd = float("-inf")
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        ticker = str(leg.get("ticker") or "").upper()
        try:
            qty = float(leg.get("qty") or 0)
            entry = float(leg.get("entry") or 0)
            px = float(prices.get(ticker) or 0)
        except (TypeError, ValueError):
            continue
        if not ticker or qty <= 0 or entry <= 0 or px <= 0:
            continue
        pnl_usd = (px - entry) * qty
        if pnl_usd > best_pnl_usd:
            best_pnl_usd = pnl_usd
            best_leg = leg
    return best_leg


def orderbook_obi(
    bids: list[Any],
    asks: list[Any],
    *,
    depth: int = 5,
) -> float | None:
    """Дисбаланс стакана L2 (0..1): доля bid-объёма в USD среди топ-depth уровней."""
    cap = max(1, int(depth))
    bid_usd = 0.0
    ask_usd = 0.0
    for row in (bids or [])[:cap]:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            try:
                bid_usd += float(row[0]) * float(row[1])
            except (TypeError, ValueError):
                continue
    for row in (asks or [])[:cap]:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            try:
                ask_usd += float(row[0]) * float(row[1])
            except (TypeError, ValueError):
                continue
    tot = bid_usd + ask_usd
    if tot <= 0:
        return None
    return bid_usd / tot


def btc_dip_exit_reason(
    *,
    price: float,
    entry: float,
    exit_arm: dict[str, Any] | None = None,
    now: float | None = None,
    swing_low: float = 0.0,
    scratch: bool = False,
    bid: float = 0.0,
    obi: float | None = None,
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
    if pocket_exit_ready(exit_arm, price=px, now=now, entry=ent, bid=bid, obi=obi):
        return "вершина"
    if scratch and gain >= POCKET_SCRATCH_PCT - 1e-6:
        return "безубыток"
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
    """Устарело: второй карман больше не покупает на фиксированную сумму."""
    return float(cfg().sui_usd)


def second_pocket_choice() -> str:
    """Устарело: тикер второго кармана приходит с щупа, не из whitelist."""
    raw = str(cfg().second_ticker or "AVA").upper()
    return raw if raw in {"SUI", "AVA"} else "AVA"


def pocket_from_probe_max() -> int:
    """Сколько ног, переданных с щупа, держит второй карман."""
    return max(1, int(cfg().pocket_from_probe_max))


# Ручные покупки (кроме BTC) сажаем в карман2 под ту же ловушку вершины.
MANUAL_TRAP_SKIP = frozenset({"BTC"})
MANUAL_TRAP_MAX_LEGS = 32


def manual_trap_free_qty(
    held_qty: float,
    *,
    probe_qty: float = 0.0,
    pocket_qty: float = 0.0,
    dip_qty: float = 0.0,
) -> float:
    """Сколько монет на балансе ещё не учтено щупом/карманом — кандидат на ловушку."""
    free = float(held_qty or 0) - float(probe_qty or 0) - float(pocket_qty or 0) - float(dip_qty or 0)
    return free if free > 1e-12 else 0.0


def probe_should_handoff(reason: str | None) -> bool:
    """Мягкий выход щупа — отдать ногу во второй карман, не продавать."""
    return str(reason or "") in PROBE_HANDOFF_REASONS


def probe_night_halted(now: float | None = None) -> bool:
    """Ночное окно щупа 23:00–06:00 YEKT: не стоп, а узкий фильтр (сильный сетап + $250k)."""
    ts = time.time() if now is None else float(now)
    hr = datetime.fromtimestamp(ts, _YEKT).hour
    return hr >= PROBE_NIGHT_START_HOUR_YEKT or hr < PROBE_NIGHT_END_HOUR_YEKT


def probe_night_halt_reason(now: float | None = None) -> str | None:
    """Полный ночной стоп снят — новые входы ночью режет probe_setup_ok_now, не пауза."""
    del now
    return None


def tape_btc_day_chg(rows: list[dict[str, Any]] | None) -> float | None:
    """Суточная процентная смена BTC из ленты тикеров. Нет строки — None."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("ticker") or "").upper() != "BTC":
            continue
        try:
            return float(row.get("chg"))
        except (TypeError, ValueError):
            return None
    return None


def probe_max_legs(*, btc_day_chg: float | None = None, now: float | None = None) -> int:
    """Днём до 4 ног. Ночью с пином: 1 пин + 1 бродячий."""
    del btc_day_chg
    if PROBE_PIN_ON and probe_night_halted(now):
        return int(PROBE_NIGHT_PIN_MAX_LEGS)
    return int(PROBE_MAX_LEGS)


def probe_setup_hold_sec(setup: dict[str, Any] | None) -> float:
    """0 — валидный hold (spark сразу). Не подменять через `or` на 90 с."""
    if not isinstance(setup, dict) or "hold_sec" not in setup or setup.get("hold_sec") is None:
        return float(PROBE_ENTRY_HOLD_SEC)
    return float(setup.get("hold_sec") or 0)


def probe_setup_ok_now(
    setup: dict[str, Any] | None,
    *,
    now: float | None = None,
    turnover: float | None = None,
    strict: bool = False,
) -> bool:
    """Ночь: сильный сетап и ≥ $250k. Жёсткий режим: то же и ≥ $1.5M."""
    if not setup:
        return False
    if not strict and not probe_night_halted(now):
        return True
    strong = (
        float(setup.get("stair") or 0) >= 1.0 - 1e-12
        or float(setup.get("impulse") or 0) >= 1.0 - 1e-12
        or float(setup.get("spark") or 0) >= 1.0 - 1e-12
    )
    if not strong:
        return False
    try:
        turn = float(turnover) if turnover is not None else 0.0
    except (TypeError, ValueError):
        turn = 0.0
    floor = PROBE_STRICT_MIN_TURNOVER_USD if strict else PROBE_NIGHT_MIN_TURNOVER_USD
    return turn >= float(floor) - 1e-9


def probe_min_turnover_usd(now: float | None = None, *, strict: bool = False) -> float:
    """Порог оборота: день $150k, ночь $250k, жёсткий режим $1.5M."""
    if strict:
        return float(PROBE_STRICT_MIN_TURNOVER_USD)
    if now is not None and probe_night_halted(now):
        return float(PROBE_NIGHT_MIN_TURNOVER_USD)
    return float(PROBE_MIN_TURNOVER_USD)


def _tape_liquid_day_chgs(rows: list[dict[str, Any]] | None) -> list[float]:
    chgs: list[float] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in SLEEVE_SKIP or _leveraged_base(ticker):
            continue
        try:
            chg = float(row.get("chg") or 0)
            turn = float(row.get("turnover") or 0)
        except (TypeError, ValueError):
            continue
        if turn >= PROBE_MIN_TURNOVER_USD:
            chgs.append(chg)
    return chgs


def probe_market_rising(rows: list[dict[str, Any]] | None) -> bool:
    """Рынок растёт: BTC за сутки ≥ 0 или ≥ половины ликвидных имён в плюсе."""
    btc = tape_btc_day_chg(rows)
    if btc is not None and btc >= PROBE_RISING_BTC_MIN_PCT - 1e-12:
        return True
    liquid = _tape_liquid_day_chgs(rows)
    if len(liquid) < 20:
        return False
    green_pct = sum(1 for chg in liquid if chg > 0) / len(liquid) * 100.0
    return green_pct >= PROBE_RISING_BREADTH_PCT - 1e-9


def probe_strict_reason(
    reason: str | None,
    rows: list[dict[str, Any]] | None = None,
) -> str | None:
    """Жёсткий режим только если включён и рынок не растёт."""
    if not PROBE_STRICT_MODE or not reason:
        return None
    if probe_market_rising(rows):
        return None
    return reason


def probe_market_regime_ok(rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """Защита от падающего рынка: стоп щупа при сливе BTC или широком обвале спота."""
    liquid_chgs: list[float] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in SLEEVE_SKIP or _leveraged_base(ticker):
            continue
        try:
            chg = float(row.get("chg") or 0)
            turn = float(row.get("turnover") or 0)
        except (TypeError, ValueError):
            continue
        if ticker == "BTC" and chg <= PROBE_BTC_MAX_DROP_DAY_PCT + 1e-9:
            return False, f"BTC {chg:+.1f}% ≤ {PROBE_BTC_MAX_DROP_DAY_PCT:.1f}%"
        if turn >= PROBE_MIN_TURNOVER_USD:
            liquid_chgs.append(chg)
    if len(liquid_chgs) >= 20:
        green_pct = sum(1 for c in liquid_chgs if c > 0) / len(liquid_chgs) * 100.0
        if green_pct < PROBE_MIN_MARKET_BREADTH_PCT - 1e-9:
            return False, f"ширина рынка {green_pct:.0f}% < {PROBE_MIN_MARKET_BREADTH_PCT:.0f}%"
    return True, ""


def probe_market_regime_pause_reason(
    rows: list[dict[str, Any]],
    *,
    now: float,
    pause_until: float = 0.0,
) -> tuple[str | None, float]:
    """Падающий рынок: при срабатывании — пауза новых щупов на PROBE_MARKET_FALL_PAUSE_SEC."""
    ok, note = probe_market_regime_ok(rows)
    until = float(pause_until or 0.0)
    if not ok:
        return f"падающий рынок: {note}", float(now) + float(PROBE_MARKET_FALL_PAUSE_SEC)
    if until > float(now) + 1e-9:
        left_min = max(1, int((until - float(now) + 59) // 60))
        return f"падающий рынок: пауза {left_min}м после обвала", until
    return None, 0.0


def probe_scan_rank(
    row: dict[str, Any],
    *,
    min_turnover: float | None = None,
) -> float:
    """Сортировка scan: рост за 24ч (chg %), только ликвидные и day ≥ PROBE_DAY_MIN_PCT."""
    floor_turn = float(PROBE_MIN_TURNOVER_USD if min_turnover is None else min_turnover)
    try:
        chg = float(row.get("chg") or 0)
        price = float(row.get("price") or 0)
        turn = float(row.get("turnover") or 0)
    except (TypeError, ValueError):
        return -1.0
    if turn < floor_turn or price <= 0 or chg < PROBE_DAY_MIN_PCT:
        return float("-inf")
    return chg


def probe_scan_ranked_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int | None = None,
    now: float | None = None,
    min_turnover: float | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    """Список для щупа: топ-50 по 24h %, затем высоколиквидные ($1.5M+) в фазе разгона, затем остальные."""
    cap = int(PROBE_SCAN_LIMIT if limit is None else limit)
    floor_turn = (
        float(min_turnover)
        if min_turnover is not None
        else probe_min_turnover_usd(now)
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        score = probe_scan_rank(row, min_turnover=floor_turn)
        if score < PROBE_DAY_MIN_PCT:
            continue
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) <= PROBE_SCAN_MANDATORY_TOP:
        return scored[:cap]
    head = scored[:PROBE_SCAN_MANDATORY_TOP]
    rest = scored[PROBE_SCAN_MANDATORY_TOP:]
    prime: list[tuple[float, dict[str, Any]]] = []
    other: list[tuple[float, dict[str, Any]]] = []
    for item in rest:
        sc, r = item
        turn = float(r.get("turnover") or 0)
        if turn >= 1_500_000.0 and 1.5 <= sc <= 25.0:
            prime.append(item)
        else:
            other.append(item)
    prime.sort(key=lambda item: (float(item[1].get("turnover") or 0), item[0]), reverse=True)
    combined = head + prime + other
    return combined[:cap]


def probe_pin_room_pct(row: dict[str, Any] | None) -> float:
    """Запас до хая суток, %."""
    if not isinstance(row, dict):
        return -1.0
    try:
        price = float(row.get("price") or 0)
        high = float(row.get("high") or 0)
    except (TypeError, ValueError):
        return -1.0
    if price <= 0 or high <= 0:
        return -1.0
    return (high - price) / high * 100.0


def probe_day_ranks(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    """Место по росту за 24ч среди ликвидных имён (1 — лидер)."""
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in seen or ticker in SLEEVE_SKIP or _leveraged_base(ticker):
            continue
        try:
            chg = float(row.get("chg") or 0)
            turn = float(row.get("turnover") or 0)
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if turn < PROBE_MIN_TURNOVER_USD or price <= 0:
            continue
        seen.add(ticker)
        ranked.append((chg, ticker))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return {ticker: index + 1 for index, (_chg, ticker) in enumerate(ranked)}


def probe_pin_row_ok(row: dict[str, Any] | None) -> bool:
    """Кандидат пина: ликвидный продолжатель, не парабола и не у самого хая."""
    if not isinstance(row, dict):
        return False
    ticker = str(row.get("ticker") or "").upper()
    if not ticker or ticker in SLEEVE_SKIP or _leveraged_base(ticker):
        return False
    try:
        chg = float(row.get("chg") or 0)
        turn = float(row.get("turnover") or 0)
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        return False
    if price <= 0 or turn < PROBE_PIN_MIN_TURNOVER_USD - 1e-9:
        return False
    if chg < PROBE_PIN_MIN_DAY_PCT - 1e-12 or chg > PROBE_PIN_MAX_DAY_PCT + 1e-12:
        return False
    room = probe_pin_room_pct(row)
    return PROBE_PIN_MIN_ROOM_PCT - 1e-12 <= room <= PROBE_PIN_MAX_ROOM_PCT + 1e-12


def probe_pin_score(row: dict[str, Any], *, rank: int) -> float:
    """Скор пина: место в топе + рост + воздух до хая + оборот сверх пола."""
    try:
        chg = float(row.get("chg") or 0)
        turn = float(row.get("turnover") or 0)
    except (TypeError, ValueError):
        return 0.0
    span = max(1e-9, PROBE_PIN_MAX_DAY_PCT - PROBE_PIN_MIN_DAY_PCT)
    room_span = max(1e-9, PROBE_PIN_MAX_ROOM_PCT - PROBE_PIN_MIN_ROOM_PCT)
    rank_pts = max(0.0, (PROBE_PIN_TOP_N - int(rank) + 1) / PROBE_PIN_TOP_N) * 40.0
    chg_pts = max(0.0, min(1.0, (chg - PROBE_PIN_MIN_DAY_PCT) / span)) * 25.0
    room_pts = max(0.0, min(1.0, (probe_pin_room_pct(row) - PROBE_PIN_MIN_ROOM_PCT) / room_span)) * 25.0
    turn_pts = max(0.0, min(10.0, (turn / PROBE_PIN_MIN_TURNOVER_USD - 1.0) * 2.0))
    return rank_pts + chg_pts + room_pts + turn_pts


def probe_pin_hard_drop_reason(
    row: dict[str, Any] | None,
    *,
    ticker: str,
    rank: int | None,
    lost: set[str] | None = None,
) -> str | None:
    """Снять пин сразу: срез, нет воздуха, выпал из топа, тонкий стакан, перегрев."""
    name = str(ticker or "").upper()
    if not name:
        return "пусто"
    if lost and name in lost:
        return "убыток щупа"
    if not isinstance(row, dict):
        return "нет в ленте"
    try:
        chg = float(row.get("chg") or 0)
        turn = float(row.get("turnover") or 0)
    except (TypeError, ValueError):
        return "нет в ленте"
    if turn < PROBE_MIN_TURNOVER_USD - 1e-9:
        return "оборот"
    if chg >= PROBE_CANDLE_MAX_DAY_PCT - 1e-12:
        return "перегрев"
    room = probe_pin_room_pct(row)
    if room < PROBE_PIN_DROP_ROOM_PCT - 1e-12:
        return "нет воздуха"
    if room > PROBE_PIN_MAX_ROOM_PCT + 1e-12:
        return "слив с хая"
    if chg < PROBE_PIN_MIN_DAY_PCT - 1e-12:
        return "сутки ослабли"
    if rank is None or int(rank) > PROBE_PIN_TOP_N:
        return "вне топа"
    return None


def probe_choose_pin(
    rows: list[dict[str, Any]] | None,
    current: dict[str, Any] | None = None,
    *,
    now: float,
    lost: set[str] | None = None,
    skip: set[str] | None = None,
) -> dict[str, Any] | None:
    """Выбрать или удержать мягкий пин. Пусто — бродячие слоты как обычно."""
    if not PROBE_PIN_ON:
        return None
    lost_names = {str(name).upper() for name in (lost or set()) if name}
    skip_names = {str(name).upper() for name in (skip or set()) if name}
    ranks = probe_day_ranks(rows)
    by_ticker: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if ticker:
            by_ticker[ticker] = row
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for ticker, row in by_ticker.items():
        if ticker in lost_names or ticker in skip_names:
            continue
        rank = ranks.get(ticker)
        if rank is None or rank > PROBE_PIN_TOP_N:
            continue
        if not probe_pin_row_ok(row):
            continue
        score = probe_pin_score(row, rank=rank)
        if score < PROBE_PIN_MIN_SCORE - 1e-12:
            continue
        scored.append((score, ticker, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    cur = current if isinstance(current, dict) else {}
    cur_ticker = str(cur.get("ticker") or "").upper()
    try:
        cur_ts = float(cur.get("ts") or 0)
        cur_score = float(cur.get("score") or 0)
    except (TypeError, ValueError):
        cur_ts = 0.0
        cur_score = 0.0
    age = (float(now) - cur_ts) if cur_ticker and cur_ts > 0 else 1e18
    cur_row = by_ticker.get(cur_ticker)
    cur_rank = ranks.get(cur_ticker)
    forced = bool(cur.get("forced"))
    if cur_ticker and forced and age < PROBE_PIN_HOLD_SEC:
        try:
            turn = float((cur_row or {}).get("turnover") or 0)
        except (TypeError, ValueError):
            turn = 0.0
        if isinstance(cur_row, dict) and turn >= PROBE_MIN_TURNOVER_USD - 1e-9:
            return {
                "ticker": cur_ticker,
                "ts": cur_ts,
                "score": cur_score,
                "forced": True,
            }
    drop = (
        probe_pin_hard_drop_reason(cur_row, ticker=cur_ticker, rank=cur_rank, lost=lost_names)
        if cur_ticker
        else "пусто"
    )
    if cur_ticker and cur_ticker not in skip_names and drop is None:
        live_score = (
            probe_pin_score(cur_row, rank=int(cur_rank))
            if cur_row and cur_rank
            else cur_score
        )
        strong = (
            int(cur_rank or 999) <= PROBE_PIN_KEEP_TOP_N
            and live_score >= PROBE_PIN_MIN_SCORE - 1e-12
        )
        if strong:
            return {"ticker": cur_ticker, "ts": cur_ts, "score": live_score}
        if age < PROBE_PIN_HOLD_SEC and live_score >= PROBE_PIN_MIN_SCORE - 1e-12:
            return {
                "ticker": cur_ticker,
                "ts": cur_ts,
                "score": live_score or cur_score,
            }
        if age < PROBE_PIN_MAX_HOLD_SEC and live_score >= PROBE_PIN_MIN_SCORE - 1e-12:
            best = scored[0] if scored else None
            if (
                best is None
                or best[1] == cur_ticker
                or best[0] <= live_score * PROBE_PIN_SWITCH_SCORE_MULT
            ):
                return {"ticker": cur_ticker, "ts": cur_ts, "score": live_score}

    if not scored:
        return None
    score, ticker, _row = scored[0]
    if ticker == cur_ticker and cur_ts > 0:
        return {"ticker": ticker, "ts": cur_ts, "score": score}
    return {"ticker": ticker, "ts": float(now), "score": score}


def probe_is_paper() -> bool:
    """Имитация: скан, взвод и логи, ордеров Buy нет. Живые ноги продаёт как обычно."""
    return bool(cfg().probe_on) and bool(cfg().probe_paper)


def probe_places_buys() -> bool:
    """Боевой щуп: настройки включены и не имитация."""
    return bool(cfg().probe_on) and not bool(cfg().probe_paper)


def probe_quote() -> float:
    """Щуп: базовый размер ноги из настроек (обычно 30$)."""
    return float(cfg().probe_usd)


PROBE_BOOST_USD = 60.0
PROBE_BOOST_MIN_GAIN_PCT = 3.0


def probe_legs_boost_ok(
    legs: list[dict[str, Any]],
    prices: dict[str, float],
) -> bool:
    """Две открытые ноги в уверенном плюсе — третью можно увеличить до PROBE_BOOST_USD."""
    if len(legs) != 2:
        return False
    for leg in legs:
        ticker = str(leg.get("ticker") or "").upper()
        try:
            entry = float(leg.get("entry") or 0)
            price = float(prices.get(ticker) or 0)
        except (TypeError, ValueError):
            return False
        if entry <= 0 or price <= 0:
            return False
        gain = (price / entry - 1.0) * 100.0
        if gain + 1e-9 < PROBE_BOOST_MIN_GAIN_PCT:
            return False
    return True


def probe_quote_for_buy(
    *,
    legs: list[dict[str, Any]] | None = None,
    prices: dict[str, float] | None = None,
) -> float:
    """1–2-я нога — база из настроек; 3-я — $60, если обе предыдущие ≥ PROBE_BOOST_MIN_GAIN_PCT%."""
    base = probe_quote()
    legs = list(legs or [])
    if len(legs) != 2:
        return base
    if prices and probe_legs_boost_ok(legs, prices):
        return max(base, float(PROBE_BOOST_USD))
    return base


def probe_reserve_quote(*, open_legs: int, pending: int = 0) -> float:
    """Пустой щуп резервирует один слот. Взвод или открытая нога держат место и под второй."""
    if not probe_places_buys():
        return 0.0
    opened = max(0, int(open_legs))
    waiting = max(0, int(pending))
    want = PROBE_MAX_LEGS if opened + waiting >= 1 else 1
    slots = max(0, want - opened)
    if slots <= 0:
        return 0.0
    unit = probe_quote()
    if opened >= 2:
        unit = max(unit, float(PROBE_BOOST_USD))
    return unit * slots


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


def probe_candle_live_ok(price: float, live_high: float, live_low: float, live_open: float) -> bool:
    """Начало восходящей свечи: тело 0.6..2.0% от open, цена в верхней части бара без слива."""
    px = float(price or 0)
    hi = float(live_high or 0)
    lo = float(live_low or 0)
    op = float(live_open or 0)
    if px <= 0 or hi <= 0 or lo <= 0 or op <= 0 or hi < lo:
        return False
    if px <= op + 1e-12:
        return False
    body = (px / op - 1.0) * 100.0
    min_body = probe_candle_min_body_pct()
    if body < min_body or body > PROBE_CANDLE_MAX_BODY_PCT + 1e-12:
        return False
    # Откат сверху от high свечи (верхний фитиль) — признак уже продавленной свечи.
    upper_wick = (hi - px) / op * 100.0
    if upper_wick > PROBE_CANDLE_MAX_WICK_PCT + 1e-12:
        return False
    tip_cap = hi * (1.0 - PROBE_CANDLE_TIP_GAP_PCT / 100.0)
    if px > tip_cap + 1e-12:
        return False
    # Глубокий пролив под open — не чистый старт восходящей свечи.
    if lo < op:
        dip = (op - lo) / op * 100.0
        if dip > PROBE_CANDLE_MAX_DIP_PCT + 1e-12:
            return False
    span = hi - lo
    if span > 1e-12:
        pos = (px - lo) / span
        if pos < PROBE_CANDLE_MIN_BAR_POS - 1e-12 or pos > PROBE_CANDLE_MAX_BAR_POS + 1e-12:
            return False
    return True


def probe_spark_live_ok(price: float, live_high: float, live_low: float, live_open: float) -> bool:
    """Первые 1–2 мин 5м: тело 0.6..2.0%, можно у хая бара, без слива под open."""
    px = float(price or 0)
    hi = float(live_high or 0)
    lo = float(live_low or 0)
    op = float(live_open or 0)
    if px <= 0 or hi <= 0 or lo <= 0 or op <= 0 or hi < lo:
        return False
    if px <= op + 1e-12:
        return False
    body = (px / op - 1.0) * 100.0
    min_body = probe_candle_min_body_pct()
    if body < min_body or body > PROBE_CANDLE_MAX_BODY_PCT + 1e-12:
        return False
    upper_wick = (hi - px) / op * 100.0
    if upper_wick > PROBE_CANDLE_MAX_WICK_PCT + 1e-12:
        return False
    if lo < op:
        dip = (op - lo) / op * 100.0
        if dip > PROBE_CANDLE_MAX_DIP_PCT + 1e-12:
            return False
    span = hi - lo
    if span > 1e-12:
        pos = (px - lo) / span
        if pos < PROBE_CANDLE_MIN_BAR_POS - 1e-12 or pos > PROBE_SPARK_MAX_BAR_POS + 1e-12:
            return False
    return True


def probe_spark_buy_ok(
    price: float,
    live_high: float,
    live_low: float,
    live_open: float,
    *,
    elapsed_sec: float | None = None,
) -> bool:
    """Перед buy spark: окно 2 мин ещё живо и свеча не слилась."""
    if elapsed_sec is not None and float(elapsed_sec) > PROBE_SPARK_MAX_ELAPSED_SEC + 1e-12:
        return False
    return probe_spark_live_ok(price, live_high, live_low, live_open)


def probe_spark_setup(
    bars: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> dict[str, float] | None:
    """Одиночная 5м в первые 1–2 минуты бара. Без потолка 30м +2.2%."""
    if now is None or len(bars) < PROBE_CANDLE_LOOKBACK + 2:
        return None
    forming = bars[-1]
    window = bars[-(PROBE_CANDLE_LOOKBACK + 1):-1]
    try:
        op = float(forming.get("open") or 0)
        hi = float(forming.get("high") or 0)
        lo = float(forming.get("low") or 0)
        px = float(forming.get("close") or 0)
        vol = float(forming.get("volume") or 0)
        bar_ts = float(forming.get("ts") or 0)
        vols = [float(bar["volume"]) for bar in window if float(bar.get("volume") or 0) > 0]
    except (TypeError, ValueError, KeyError):
        return None
    if bar_ts <= 0 or not vols:
        return None
    elapsed = float(now) - bar_ts
    if elapsed < 0.0 or elapsed > PROBE_SPARK_MAX_ELAPSED_SEC + 1e-12:
        return None
    typical = _median(vols)
    if typical <= 0:
        return None
    elapsed_frac = max(PROBE_CANDLE_MIN_ELAPSED_FRAC, min(1.0, elapsed / 300.0))
    if vol < typical * PROBE_CANDLE_VOL_MULT * elapsed_frac:
        return None
    if not probe_spark_live_ok(px, hi, lo, op):
        return None
    return {"shelf": op, "price": px, "spark": 1.0}


def probe_candle_buy_ok(price: float, live_high: float, live_low: float, live_open: float) -> bool:
    """Перед buy после arm: свеча всё ещё восходящая, не у самого кончика (кроме плотной ступеньки) и без слива."""
    px = float(price or 0)
    hi = float(live_high or 0)
    lo = float(live_low or 0)
    op = float(live_open or 0)
    if px <= 0 or hi <= 0 or op <= 0:
        return False
    if px <= op + 1e-12:
        return False
    body = (px / op - 1.0) * 100.0
    if body > PROBE_CANDLE_MAX_BODY_PCT * 1.5 + 1e-12:
        return False
    upper_wick = (hi - px) / op * 100.0
    pos = (px - lo) / (hi - lo) if (lo > 0 and hi > lo + 1e-12) else 1.0
    tight_stair = (
        PROBE_STAIR_MIN_BODY_PCT <= body <= PROBE_STAIR_MAX_BODY_PCT + 1e-12
        and upper_wick <= PROBE_STAIR_MAX_WICK_PCT + 1e-12
        and pos >= PROBE_STAIR_MIN_BAR_POS - 1e-12
    )
    tip_cap = hi * (1.0 - PROBE_CANDLE_TIP_GAP_PCT / 100.0)
    if px > tip_cap + 1e-12 and not tight_stair:
        return False
    if lo > 0 and hi > lo + 1e-12:
        if pos < PROBE_CANDLE_MIN_BAR_POS - 1e-12:
            return False
    return True


def probe_book_buy_ok(
    *,
    bid: float,
    ask: float,
    arm_px: float = 0.0,
    strict: bool = False,
) -> bool:
    """Проверка живого стакана перед рыночным Buy: спред и ask-slip. В жёстком режиме туже."""
    b = float(bid or 0)
    a = float(ask or 0)
    if b <= 0 or a <= 0 or a < b:
        return True
    spread_pct = (a - b) / b * 100.0
    max_spread = PROBE_STRICT_MAX_SPREAD_PCT if strict else PROBE_MAX_SPREAD_PCT
    max_slip = PROBE_STRICT_MAX_ASK_SLIP_PCT if strict else PROBE_MAX_ASK_SLIP_PCT
    if spread_pct > max_spread + 1e-12:
        return False
    ref = float(arm_px or 0)
    if ref > 0 and (a / ref - 1.0) * 100.0 > max_slip + 1e-12:
        return False
    return True


def probe_entry_obi_floor(*, strict: bool = False) -> float:
    """Порог OBI: обычный вход без фильтра, жёсткий режим — 0.70."""
    if not strict:
        return 0.0
    return float(PROBE_STRICT_MIN_ENTRY_OBI)


def probe_entry_obi_ok(obi: float | None, *, strict: bool = False) -> bool:
    """OBI перед Buy только в жёстком режиме. Нет котировки стакана — не блокируем."""
    if not strict:
        return True
    if obi is None:
        return True
    try:
        value = float(obi)
    except (TypeError, ValueError):
        return True
    return value >= probe_entry_obi_floor(strict=True) - 1e-12


def _probe_staircase_setup(
    bars: list[dict[str, Any]],
    *,
    typical_vol: float,
    elapsed_frac: float = 1.0,
) -> dict[str, float] | None:
    """Ступенька из 3 зелёных 5м свечей с растущими close и непадающими low (TREE, BIRB, WET, MPLX)."""
    if len(bars) < 3 or typical_vol <= 0:
        return None
    b2, prior, forming = bars[-3], bars[-2], bars[-1]
    try:
        op = float(forming.get("open") or 0)
        hi = float(forming.get("high") or 0)
        lo = float(forming.get("low") or 0)
        px = float(forming.get("close") or 0)
        vol = float(forming.get("volume") or 0)
        p_op = float(prior.get("open") or 0)
        p_lo = float(prior.get("low") or 0)
        p_cl = float(prior.get("close") or 0)
        p_vol = float(prior.get("volume") or 0)
        b2_op = float(b2.get("open") or 0)
        b2_lo = float(b2.get("low") or 0)
        b2_cl = float(b2.get("close") or 0)
    except (TypeError, ValueError, KeyError):
        return None
    if min(op, hi, lo, px, p_op, p_lo, p_cl, b2_op, b2_lo, b2_cl) <= 0 or hi < lo:
        return None
    if not (px > op and p_cl > p_op and b2_cl >= b2_op):
        return None
    if not (px > p_cl > b2_cl):
        return None
    if lo < p_lo - 1e-12 or p_lo < b2_lo - 1e-12:
        return None
    rise3 = (px / b2_op - 1.0) * 100.0
    if rise3 < PROBE_STAIR_MIN_RISE_PCT - 1e-12 or rise3 > PROBE_STAIR_MAX_RISE_PCT + 1e-12:
        return None
    body = (px / op - 1.0) * 100.0
    if body < PROBE_STAIR_MIN_BODY_PCT - 1e-12 or body > PROBE_STAIR_MAX_BODY_PCT + 1e-12:
        return None
    span = hi - lo
    pos = (px - lo) / span if span > 1e-12 else 1.0
    if pos < PROBE_STAIR_MIN_BAR_POS - 1e-12:
        return None
    wick = (hi - px) / op * 100.0
    if wick > PROBE_STAIR_MAX_WICK_PCT + 1e-12:
        return None
    if lo < op and (op - lo) / op * 100.0 > PROBE_STAIR_MAX_DIP_PCT + 1e-12:
        return None
    vol2_avg = (vol + p_vol) / 2.0
    if vol2_avg < typical_vol * PROBE_STAIR_VOL2_MULT * elapsed_frac:
        return None
    if vol < typical_vol * PROBE_STAIR_VOL_MIN_MULT * elapsed_frac:
        return None
    window = bars[-(PROBE_CANDLE_LOOKBACK + 1):-1]
    if window:
        try:
            hi_1h = max(float(bar["high"]) for bar in window)
            lo_1h = min(float(bar["low"]) for bar in window)
        except (TypeError, ValueError, KeyError):
            return None
        if hi_1h > lo_1h + 1e-12:
            pos_1h = (px - lo_1h) / (hi_1h - lo_1h)
            if pos_1h < PROBE_STAIR_MIN_1H_POS - 1e-12:
                return None
    return {"shelf": p_op, "price": px, "stair": 1.0}


def _probe_staircase_2bar_setup(
    bars: list[dict[str, Any]],
    *,
    typical_vol: float,
    elapsed_frac: float = 1.0,
) -> dict[str, float] | None:
    """Ранний вход на 2-й ступеньке (prior зелёная + forming пробивает prior.high, суммарный ход 0.80..1.80%)."""
    if len(bars) < 2 or typical_vol <= 0:
        return None
    prior, forming = bars[-2], bars[-1]
    try:
        op = float(forming.get("open") or 0)
        hi = float(forming.get("high") or 0)
        lo = float(forming.get("low") or 0)
        px = float(forming.get("close") or 0)
        vol = float(forming.get("volume") or 0)
        p_op = float(prior.get("open") or 0)
        p_hi = float(prior.get("high") or 0)
        p_lo = float(prior.get("low") or 0)
        p_cl = float(prior.get("close") or 0)
        p_vol = float(prior.get("volume") or 0)
    except (TypeError, ValueError, KeyError):
        return None
    if min(op, hi, lo, px, p_op, p_hi, p_lo, p_cl) <= 0 or hi < lo:
        return None
    if not (px > op and p_cl > p_op and px > p_hi):
        return None
    if lo < p_lo - 1e-12:
        return None
    rise2 = (px / p_op - 1.0) * 100.0
    if rise2 < PROBE_STAIR2_MIN_RISE_PCT - 1e-12 or rise2 > PROBE_STAIR2_MAX_RISE_PCT + 1e-12:
        return None
    body = (px / op - 1.0) * 100.0
    if body < PROBE_STAIR2_MIN_BODY_PCT - 1e-12 or body > PROBE_STAIR2_MAX_BODY_PCT + 1e-12:
        return None
    span = hi - lo
    pos = (px - lo) / span if span > 1e-12 else 1.0
    if pos < PROBE_STAIR2_MIN_BAR_POS - 1e-12:
        return None
    wick = (hi - px) / op * 100.0
    if wick > PROBE_STAIR2_MAX_WICK_PCT + 1e-12:
        return None
    if lo < op and (op - lo) / op * 100.0 > PROBE_STAIR2_MAX_DIP_PCT + 1e-12:
        return None
    vol2_avg = (vol + p_vol) / 2.0
    if vol2_avg < typical_vol * PROBE_STAIR2_VOL2_MULT * elapsed_frac:
        return None
    if vol < typical_vol * PROBE_STAIR2_VOL_MIN_MULT * elapsed_frac:
        return None
    window = bars[-(PROBE_CANDLE_LOOKBACK + 1):-1]
    if window:
        try:
            hi_1h = max(float(bar["high"]) for bar in window)
            lo_1h = min(float(bar["low"]) for bar in window)
        except (TypeError, ValueError, KeyError):
            return None
        if hi_1h > lo_1h + 1e-12:
            pos_1h = (px - lo_1h) / (hi_1h - lo_1h)
            if pos_1h < PROBE_STAIR2_MIN_1H_POS - 1e-12:
                return None
    return {"shelf": p_op, "price": px, "stair": 1.0}


def _probe_flag_setup(
    bars: list[dict[str, Any]],
    *,
    typical_vol: float,
    elapsed_frac: float = 1.0,
) -> dict[str, float] | None:
    """Пробой 15-минутного плоского флага у вершины часа (TRUMP 19:40 после полки 19:20–19:35)."""
    if len(bars) < PROBE_CANDLE_LOOKBACK + 2 or typical_vol <= 0:
        return None
    forming = bars[-1]
    flag = bars[-4:-1]
    window = bars[-(PROBE_CANDLE_LOOKBACK + 1):-1]
    try:
        op = float(forming.get("open") or 0)
        hi = float(forming.get("high") or 0)
        lo = float(forming.get("low") or 0)
        px = float(forming.get("close") or 0)
        vol = float(forming.get("volume") or 0)
        flag_top = max(max(float(b["open"]), float(b["close"])) for b in flag)
        flag_bot = min(min(float(b["open"]), float(b["close"])) for b in flag)
        flag_hi = max(float(b["high"]) for b in flag)
        hi_1h = max(float(b["high"]) for b in window)
        lo_1h = min(float(b["low"]) for b in window)
    except (TypeError, ValueError, KeyError):
        return None
    if min(op, hi, lo, px, flag_top, flag_bot, flag_hi, hi_1h, lo_1h) <= 0 or hi < lo:
        return None
    if px <= op or px <= flag_hi:
        return None
    body = (px / op - 1.0) * 100.0
    if body < PROBE_CANDLE_MIN_BODY_PCT - 1e-12 or body > PROBE_CANDLE_MAX_BODY_PCT + 1e-12:
        return None
    if (hi - px) / op * 100.0 > 0.50 + 1e-12:
        return None
    if lo < op and (op - lo) / op * 100.0 > PROBE_STAIR_MAX_DIP_PCT + 1e-12:
        return None
    span = hi - lo
    pos = (px - lo) / span if span > 1e-12 else 1.0
    if pos < 0.65 - 1e-12:
        return None
    if (flag_top / flag_bot - 1.0) * 100.0 > 0.80 + 1e-12:
        return None
    if hi_1h <= lo_1h + 1e-12 or (px - lo_1h) / (hi_1h - lo_1h) < PROBE_STAIR_MIN_1H_POS - 1e-12:
        return None
    # Древко флага: до плоской полки должен быть реальный импульс ≥ +2.5% от минимума часа.
    if (flag_bot / lo_1h - 1.0) * 100.0 < 2.5 - 1e-12:
        return None
    if vol < typical_vol * 1.3 * elapsed_frac:
        return None
    return {"shelf": flag_bot, "price": px, "stair": 1.0}


def probe_candle_setup(
    bars: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> dict[str, float] | None:
    """Вход: ступенька 2/3 бара, иначе пробой флага.

    Одиночная 5м свеча выключена: главный поставщик ложных полок.
    bars — 5м, от старых к новым; нужен open (опционально ts старта бара).
    Полка — open предыдущей ступени или основание флага.
    """
    need = PROBE_CANDLE_LOOKBACK + 2
    if len(bars) < need:
        return None
    forming = bars[-1]
    prior = bars[-2]
    window = bars[-(PROBE_CANDLE_LOOKBACK + 1):-1]
    try:
        op = float(forming.get("open") or 0)
        hi = float(forming.get("high") or 0)
        lo = float(forming.get("low") or 0)
        price = float(forming.get("close") or 0)
        vol = float(forming.get("volume") or 0)
        bar_ts = float(forming.get("ts") or 0)
        prior_o = float(prior.get("open") or prior.get("close") or 0)
        prior_c = float(prior.get("close") or 0)
        prior_vol = float(prior.get("volume") or 0)
        vols = [float(bar["volume"]) for bar in window if float(bar.get("volume") or 0) > 0]
    except (TypeError, ValueError, KeyError):
        return None
    if not vols:
        return None
    typical = _median(vols)
    if typical <= 0:
        return None
    # Отсечение кульминационного выброса объёма на предыдущей свече (Blow-off Volume > 6.5x).
    if prior_vol > typical * PROBE_CANDLE_MAX_PRIOR_VOL_MULT + 1e-12:
        return None
    # Проверим, стояла ли цена последние 15 минут (3 закрытых бара) в узкой полке флага ≤ 0.80% после импульса ≥ +2.5%.
    flag_cooled = False
    if len(bars) >= 4 and window:
        try:
            f_bars = bars[-4:-1]
            f_top = max(max(float(b["open"]), float(b["close"])) for b in f_bars)
            f_bot = min(min(float(b["open"]), float(b["close"])) for b in f_bars)
            lo_1h = min(float(b["low"]) for b in window)
            flag_cooled = (
                f_bot > 0
                and lo_1h > 0
                and (f_top / f_bot - 1.0) * 100.0 <= 0.80 + 1e-12
                and (f_bot / lo_1h - 1.0) * 100.0 >= 2.5 - 1e-12
            )
        except (TypeError, ValueError, KeyError):
            flag_cooled = False
    # Отсечение перегрева за последние 30 минут (> +2.20%), если не было 15-минутной полки охлаждения.
    if not flag_cooled and len(window) >= 6:
        try:
            ref_30m = float(window[-6].get("close") or 0)
        except (TypeError, ValueError):
            ref_30m = 0.0
        if ref_30m > 0 and (price / ref_30m - 1.0) * 100.0 > PROBE_CANDLE_MAX_30M_CHG_PCT + 1e-12:
            return None
    # Отсечение длинных верхних фитилей продавца (> 0.85%) на 3 предыдущих 5м свечах.
    for prev_bar in bars[-4:-1]:
        try:
            pb_o = float(prev_bar.get("open") or prev_bar.get("close") or 0)
            pb_c = float(prev_bar.get("close") or 0)
            pb_h = float(prev_bar.get("high") or prev_bar.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if pb_o > 0 and pb_h > max(pb_o, pb_c):
            pb_wick = (pb_h - max(pb_o, pb_c)) / pb_o * 100.0
            if pb_wick > PROBE_CANDLE_MAX_PREV3_WICK_PCT + 1e-12:
                return None
    elapsed_frac = 1.0
    if now is not None and bar_ts > 0:
        elapsed = float(now) - bar_ts
        if 0.0 <= elapsed < 300.0:
            elapsed_frac = max(PROBE_CANDLE_MIN_ELAPSED_FRAC, min(1.0, elapsed / 300.0))
    try:
        prior_high = max(float(bar["high"]) for bar in window)
    except (TypeError, ValueError, KeyError):
        return None
    if prior_high > 0 and price > prior_high * (1.0 + PROBE_CANDLE_MAX_BODY_PCT / 100.0):
        return None
    if prior_o > 0 and prior_c > prior_o:
        prior_body = (prior_c / prior_o - 1.0) * 100.0
        if prior_body > probe_candle_prior_max_body_pct() + 1e-12:
            return None
    stair = _probe_staircase_setup(
        bars, typical_vol=typical, elapsed_frac=elapsed_frac
    ) or _probe_staircase_2bar_setup(
        bars, typical_vol=typical, elapsed_frac=elapsed_frac
    )
    if stair:
        return stair
    return _probe_flag_setup(bars, typical_vol=typical, elapsed_frac=elapsed_frac)


def probe_scan_day_ok(
    *,
    price: float,
    day_high: float,
    day_chg: float | None,
) -> bool:
    """Лента скана: режем сутки ≥45% и слив с хая >28%. Сам хай не режем — там живут выстрелы."""
    if day_chg is not None and float(day_chg) >= PROBE_CANDLE_MAX_DAY_PCT:
        return False
    hi = float(day_high or 0)
    px = float(price or 0)
    if hi <= 0 or px <= 0:
        return True
    room = (hi - px) / hi * 100.0
    return room <= PROBE_CANDLE_MAX_DAY_DROP_PCT + 1e-12


def probe_candle_day_ok(
    *,
    price: float,
    day_high: float,
    day_chg: float | None,
    stair: bool = False,
) -> bool:
    """Топ по росту идут у хая суток; отсекаем перегрев ≥45% и глубокий слив с пика (>28%)."""
    if day_chg is not None and float(day_chg) >= PROBE_CANDLE_MAX_DAY_PCT:
        return False
    hi = float(day_high or 0)
    px = float(price or 0)
    if hi <= 0 or px <= 0:
        return True
    room = (hi - px) / hi * 100.0
    if room > PROBE_CANDLE_MAX_DAY_DROP_PCT + 1e-12:
        return False
    min_room = PROBE_STAIR_MIN_DAY_ROOM_PCT if stair else PROBE_CANDLE_MIN_DAY_ROOM_PCT
    return room >= min_room - 1e-12


def probe_daily_loss_blocked(daily_realized_usd: float | None) -> bool:
    """Жёсткий дневной стоп (Daily Max Drawdown): при убытке за сутки ≤ -PROBE_DAILY_MAX_LOSS_USD новые щупы стоп."""
    if daily_realized_usd is None:
        return False
    return float(daily_realized_usd) <= -PROBE_DAILY_MAX_LOSS_USD + 1e-9


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
        live_high = float(forming.get("high") or 0)
        live_low = float(forming.get("low") or 0)
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
    # Не на самом кончике живой свечи; выстрел почти всегда у верха бара.
    if live_high > 0 and live_low > 0:
        tip_cap = live_high * (1.0 - PROBE_IMPULSE_TIP_GAP_PCT / 100.0)
        if price > tip_cap + 1e-12:
            return None
        span = live_high - live_low
        if span > 1e-12 and (price - live_low) / span > PROBE_IMPULSE_MAX_BAR_POS + 1e-12:
            return None
    return {"shelf": prior_high, "price": price, "impulse": 1.0}


def probe_grind_live_ok(price: float, live_high: float, live_low: float) -> bool:
    """Цена не у хая живой свечи и свеча не вертикальный выстрел.

    Нужно и при взводе, и перед buy: за hold цена часто доезжает до пика.
    """
    px = float(price or 0)
    hi = float(live_high or 0)
    lo = float(live_low or 0)
    if px <= 0 or hi <= 0 or lo <= 0 or hi < lo:
        return False
    live_span = (hi - lo) / lo * 100.0
    if live_span > PROBE_GRIND_MAX_LIVE_BAR_PCT + 1e-12:
        return False
    tip_cap = hi * (1.0 - PROBE_GRIND_TIP_GAP_PCT / 100.0)
    if px > tip_cap + 1e-12:
        return False
    span = hi - lo
    if span > 1e-12 and (px - lo) / span > (1.0 - PROBE_GRIND_TIP_FRAC) + 1e-12:
        return False
    return True


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
        live_high = float(forming.get("high") or 0)
        live_low = float(forming.get("low") or 0)
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
    if not probe_grind_live_ok(price, live_high, live_low):
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
    if name == PROBE_ALGO_CANDLE:
        return {
            "hold_sec": float(PROBE_CANDLE_HOLD_SEC),
            "chase_pct": float(PROBE_CANDLE_CHASE_PCT),
        }
    if name == PROBE_ALGO_IMPULSE:
        return {
            "hold_sec": float(PROBE_IMPULSE_HOLD_SEC),
            "chase_pct": float(PROBE_IMPULSE_CHASE_PCT),
        }
    if name == PROBE_ALGO_SPARK:
        return {
            "hold_sec": float(PROBE_SPARK_HOLD_SEC),
            "chase_pct": float(PROBE_SPARK_CHASE_PCT),
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
    day_high: float | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Первый сработавший по order. Живой стол: ступенька, выстрел, spark.

    Sprint/shelf/grind — только явный order= в тестах.
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
        if name == PROBE_ALGO_CANDLE:
            setup = probe_candle_setup(bars5 or [], now=now)
            if setup and not probe_candle_day_ok(
                price=float(setup.get("price") or 0),
                day_high=float(day_high or 0),
                day_chg=day_chg,
                stair=bool(setup.get("stair")),
            ):
                continue
        elif name == PROBE_ALGO_IMPULSE:
            setup = probe_impulse_setup(bars5 or [])
        elif name == PROBE_ALGO_SPARK:
            setup = probe_spark_setup(bars5 or [], now=now)
            if setup and not probe_candle_day_ok(
                price=float(setup.get("price") or 0),
                day_high=float(day_high or 0),
                day_chg=day_chg,
                stair=False,
            ):
                continue
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
    Candle: 15м может быть ещё neutral в начале свечи — лишь бы не bear.
    """
    name = str(algo or "").strip().lower()
    if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK}:
        if trend15 == "bear" or trend1h == "bear":
            return False
        if trend15 != "bull" and trend1h != "bull":
            return False
    elif trend15 != "bull" or trend1h == "bear":
        return False
    if name == PROBE_ALGO_GRIND and trend1h != "bull":
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
    name = str(algo or "").strip().lower()
    if shelf <= 0 or price <= shelf:
        return "drop"
    # Candle допускает neutral 15м (как на скане); bear — снять.
    if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK}:
        if trend15 == "bear":
            return "drop"
    elif trend15 != "bull":
        return "drop"
    if (
        name == PROBE_ALGO_IMPULSE
        and day_chg is not None
        and float(day_chg) >= PROBE_IMPULSE_MAX_DAY_PCT
    ):
        return "drop"
    if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK} and day_chg is not None and float(day_chg) >= PROBE_CANDLE_MAX_DAY_PCT:
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
    opened_ts: float = 0.0,
    now: float | None = None,
    bid: float = 0.0,
    obi: float | None = None,
) -> str | None:
    """Стоп и полка. Плюс — ловушка: пик ≥ комиссия×2+$0.12, продажа на откате −0,35% (или −0,2% при плите ask).

    Пока ловушка не взведена: shelf режется «ожиданием»/«дном»; подножка
    impulse/sprint/grind эти срезы не берёт. Большой ход (+PROBE_RUN) ещё и медвежьим 15м.
    Если нога в минусе висит ≥ PROBE_CANDLE_STALE_SEC — «застой» (handoff в карман2).
    Срез impulse/spark — только потеря полки, не минус к входу за 90 с.
    """
    px = float(price or 0)
    ent = float(entry or 0)
    level = float(shelf or 0)
    name = str(algo or "").strip().lower()
    if px <= 0 or ent <= 0:
        return None
    gain = (px / ent - 1.0) * 100.0
    if gain <= -cfg().probe_max_loss_pct:
        return "стоп"
    if level > 0 and px <= level:
        # Candle: INIT держался над open. Фитиль к open не продаём — только пробой.
        if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK}:
            floor = level * (1.0 - PROBE_CANDLE_SHELF_BREAK_PCT / 100.0)
            if px <= floor + 1e-12:
                return "полка"
        else:
            return "полка"
    high = max(float(peak or 0), px)
    arm_pct = exit_trap_arm_pct(entry=ent, qty=qty, extra_usd=PROBE_TRAP_EXTRA_USD)
    peak_gain = (high / ent - 1.0) * 100.0 if ent > 0 else 0.0
    exit_px = min(px, float(bid)) if float(bid or 0) > 0 else px
    exit_gain = (exit_px / ent - 1.0) * 100.0 if ent > 0 else 0.0
    pullback_pct = (
        POCKET_EXIT_WALL_PULLBACK_PCT
        if (obi is not None and float(obi) <= EXIT_TRAP_BEAR_OBI + 1e-12)
        else PROBE_TRAP_PULLBACK_PCT
    )
    if (
        peak_gain >= arm_pct - 1e-6
        and exit_px <= high * (1.0 - pullback_pct / 100.0) + 1e-12
        and exit_gain >= 2.0 * EXIT_FEE_SIDE * 100.0 - 1e-6
    ):
        return "вершина"
    if (
        name in {PROBE_ALGO_IMPULSE, PROBE_ALGO_SPARK}
        and float(opened_ts or 0) > 0
        and now is not None
        and float(now) - float(opened_ts) >= PROBE_FAIL_SCRATCH_SEC
        and peak_gain < arm_pct - 1e-6
    ):
        if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK}:
            floor = level * (1.0 - PROBE_CANDLE_SHELF_BREAK_PCT / 100.0) if level > 0 else 0.0
            shelf_lost = level > 0 and px <= floor + 1e-12
        else:
            shelf_lost = level > 0 and px <= level + 1e-12
        if shelf_lost:
            return "срез"
    if (
        float(opened_ts or 0) > 0
        and now is not None
        and float(now) - float(opened_ts) >= PROBE_CANDLE_STALE_SEC
        and gain <= 1e-12
        and gain < arm_pct - 1e-6
    ):
        return "застой"
    run = high >= ent * (1.0 + PROBE_RUN_PCT / 100.0)
    footboard = name in {
        PROBE_ALGO_CANDLE,
        PROBE_ALGO_IMPULSE,
        PROBE_ALGO_SPARK,
        PROBE_ALGO_SPRINT,
        PROBE_ALGO_GRIND,
    }
    if not run and not footboard:
        if float(entry_low or 0) > 0 and px <= float(entry_low):
            return "дно"
        scratch = float(arm_px or 0) or ent
        if scratch > 0 and px <= scratch:
            return "ожидание"
    # candle/grind: купить рост → ждать ловушку → продать. Soft «тренд» не режем
    # (BLEND: −0.3% bear → handoff → потом +20%). Только полка / стоп / вершина.
    if name in {PROBE_ALGO_CANDLE, PROBE_ALGO_SPARK, PROBE_ALGO_GRIND}:
        return None
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


def pocket_entry_trends_ok(
    *,
    trend15: str,
    trend1h: str,
    week_chg: float | None,
) -> bool:
    """Карман SOL: не покупать в медвежьем 15m/1h и при неделе ≤ EXCLUDE_ENTER_CHG7."""
    if trend15 == "bear" or trend1h == "bear":
        return False
    if week_chg is not None and float(week_chg) <= EXCLUDE_ENTER_CHG7:
        return False
    return True


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
    """Плюс ≥ комиссия×2+$0.12 запоминает вершину. Новая вершина сбрасывает таймер.

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
    entry: float = 0.0,
    bid: float = 0.0,
    obi: float | None = None,
) -> bool:
    """Продажа кармана: −0,35% от вершины и пауза hold (60с), с учётом bid и дисбаланса стакана L2.

    Без ожидания таймера режем сразу, если:
    - в стакане медвежья плита (obi ≤ 0.28) и откат ≥ 0.20% от вершины;
    - цена/bid откатила ≥ 1.0% от вершины (POCKET_EXIT_DEEP_PULLBACK_PCT);
    - или цена/bid вернулась ≤ точки входа (не течём под водой).
    """
    limits = cfg()
    if not state:
        return False
    high = float(state.get("high") or 0)
    raw_px = float(price or 0)
    if high <= 0 or raw_px <= 0:
        return False
    px = min(raw_px, float(bid)) if float(bid or 0) > 0 else raw_px
    bear_wall = obi is not None and float(obi) <= EXIT_TRAP_BEAR_OBI + 1e-12
    if bear_wall and px <= high * (1.0 - POCKET_EXIT_WALL_PULLBACK_PCT / 100.0) + 1e-12:
        return True
    if px > high * (1.0 - limits.pocket_exit_pullback_pct / 100.0) + 1e-12:
        return False
    ent = float(entry or 0)
    if ent > 0 and px <= ent + 1e-12:
        return True
    if px <= high * (1.0 - POCKET_EXIT_DEEP_PULLBACK_PCT / 100.0) + 1e-12:
        return True
    return _bounce_held(state, now, limits.pocket_exit_hold_sec)


def position_basis(entry: float, anchor: float | None) -> float:
    """Стоп и ловушка считаются от якоря, если он задан. Иначе от цены покупки."""
    anc = float(anchor or 0)
    if anc > 0:
        return anc
    return float(entry or 0)



def watch_period_sec(**kwargs: Any) -> float:
    return float(WATCH_PERIOD_CALM)
