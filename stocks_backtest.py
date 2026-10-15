#!/usr/bin/env python3
"""Бэктест стратегий фондового стола на голубых фишках Мосбиржи.

Две стратегии на одних данных:
  dip   — «купил дешевле — продал дороже»: вход на просадке по Боллинджеру, выход тейком/к средней.
  trend — следование за трендом: вход на пробое максимума N дней над средней, выход трейлом или пробоем минимума.

Часовые свечи ISS, счёт в рублях с лотами и N слотами, комиссия по тарифу Т-Инвест,
свободный кэш — в фонде ликвидности, дивиденды из Т-Инвест GetDividends, налог 13%.
Главная цифра — чистые рубли в торговый день против цели и против «всё в фонде».
Заявок не ставит.

    .venv/bin/python stocks_backtest.py                    # 30 000 ₽, 2 года, обе стратегии
    .venv/bin/python stocks_backtest.py --kind trend --target 30
    .venv/bin/python stocks_backtest.py --no-divs          # без токена Т-Инвест
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import os
import sys
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT_DIR, ".env"))

from skills.crypto.indicators import bollinger  # noqa: E402
from skills.stocks.desk_policy import is_risk_off  # noqa: E402

ISS = "https://iss.moex.com/iss/engines/stock/markets"
CACHE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "jarvis_bt"
TICKERS = ("SBER", "LKOH", "GAZP", "ROSN", "NVTK", "GMKN", "YDEX", "T", "VTBR", "PLZL", "MOEX", "TATN")
# % за сторону и абонплата за месяц, в котором были сделки.
TARIFFS = {"инвестор": (0.30, 0.0), "трейдер": (0.05, 390.0)}
PERIOD = 20
TF_STEP = {"1h": 1, "4h": 4}
CHANNEL_DAYS = (10, 20, 55)
MA_DAYS = (50, 100)
# История до начала теста: индикаторам тренда нужно ~100 торговых дней разгона.
WARMUP_DAYS = 160
TAX = 0.13
TRADE_DAYS_PER_YEAR = 250
# Входы только в основную сессию будней: вечер и выходные — тонкий стакан.
ENTRY_HOUR_FROM = 10
ENTRY_HOUR_TILL = 18
# После отсечки цена падает на дивиденд — это гэп, а не «дёшево».
DIV_BLACKOUT_DAYS = 3


# ---------- данные ----------


def _iss_get(url: str, params: dict[str, str]) -> dict:
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(1.0 + attempt)
    return {}


def fetch_candles(path: str, start: date, till: date, refresh: bool) -> list[list]:
    """Часовые свечи ISS: [open, close, high, low, value, volume, begin, end]."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"moex_{path.replace('/', '_')}_{start}_{till}.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 12 * 3600:
        return json.loads(cache.read_text())
    rows: list[list] = []
    offset = 0
    while True:
        data = _iss_get(
            f"{ISS}/{path}/candles.json",
            {
                "interval": "60",
                "from": start.isoformat(),
                "till": till.isoformat(),
                "start": str(offset),
                "iss.meta": "off",
            },
        )
        chunk = (data.get("candles") or {}).get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        offset += len(chunk)
        time.sleep(0.05)
    cache.write_text(json.dumps(rows))
    return rows


def fetch_lots(tickers: list[str]) -> dict[str, int]:
    data = _iss_get(
        f"{ISS}/shares/boards/TQBR/securities.json",
        {
            "iss.meta": "off",
            "iss.only": "securities",
            "securities.columns": "SECID,LOTSIZE",
            "securities": ",".join(tickers),
        },
    )
    rows = (data.get("securities") or {}).get("data") or []
    return {str(secid): int(lot or 1) for secid, lot in rows}


def fetch_dividends(tickers: list[str], start: date, till: date, refresh: bool) -> dict[str, list[list]]:
    """Тикер → [[дата, сумма на акцию до налога, strict]]. strict — дата последнего дня с дивидендом."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"divs_{start}_{till}_{'-'.join(sorted(tickers))}.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 24 * 3600:
        return json.loads(cache.read_text())
    from skills.stocks import StocksSkill
    from skills.stocks.common import _quotation_to_float

    skill = StocksSkill()
    if not skill._token:
        raise RuntimeError("нет токена Т-Инвест")
    out: dict[str, list[list]] = {}
    for ticker in tickers:
        inst = skill._instrument(None, ticker)
        ident = inst.get("uid") or inst.get("figi")
        if not ident:
            continue
        data = skill._post(
            skill._url("InstrumentsService/GetDividends"),
            {
                "instrumentId": ident,
                "from": f"{start.isoformat()}T00:00:00Z",
                "to": f"{(till + timedelta(days=1)).isoformat()}T00:00:00Z",
            },
        )
        rows: list[list] = []
        for item in data.get("dividends") or []:
            amount = _quotation_to_float(item.get("dividendNet"))
            last_buy = str(item.get("lastBuyDate") or "")[:10]
            record = str(item.get("recordDate") or "")[:10]
            if amount > 0 and (last_buy or record):
                rows.append([last_buy or record, amount, bool(last_buy)])
        out[ticker] = rows
    cache.write_text(json.dumps(out))
    return out


# ---------- подготовка ----------


@dataclass
class Bars:
    ticker: str
    lot: int
    ts: list[datetime]
    day: list[int]
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    risk: list[bool]
    z: dict[str, list[float | None]] = field(default_factory=dict)
    ma: dict[str, list[float | None]] = field(default_factory=dict)
    # По завершённым дням до текущего: максимум/минимум канала и средняя закрытий.
    ch_hi: dict[int, list[float | None]] = field(default_factory=dict)
    ch_lo: dict[int, list[float | None]] = field(default_factory=dict)
    dma: dict[int, list[float | None]] = field(default_factory=dict)
    div_on_day: dict[int, float] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)


@dataclass
class Universe:
    bars: list[Bars]
    dates: list[date]
    events: list[tuple[datetime, list[tuple[int, int]]]]
    sim_from: int
    capital: float
    slip_pct: float
    fund_pct: float


def _parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")


def risk_by_ts(rows: list[list]) -> tuple[list[datetime], list[bool]]:
    """Режим IMOEX на каждый час: те же пороги, что у фондового стола."""
    ts = [_parse_ts(r[6]) for r in rows]
    closes = [float(r[1]) for r in rows]
    day_close: dict[date, float] = {}
    for t, c in zip(ts, closes):
        day_close[t.date()] = c
    days = sorted(day_close)
    flags: list[bool] = []
    for t, c in zip(ts, closes):
        k = bisect_left(days, t.date())
        prev = day_close[days[k - 1]] if k > 0 else None
        j = bisect_right(days, t.date() - timedelta(days=7)) - 1
        week_ref = day_close[days[j]] if j >= 0 else None
        day_pct = (c / prev - 1) * 100 if prev else None
        week_pct = (c / week_ref - 1) * 100 if week_ref else None
        flags.append(is_risk_off(week=week_pct, day=day_pct))
    return ts, flags


def _bands(closes: list[float], step: int) -> tuple[list[float | None], list[float | None]]:
    n = len(closes)
    z: list[float | None] = [None] * n
    ma: list[float | None] = [None] * n
    span = step * (PERIOD - 1)
    for i in range(span, n):
        band = bollinger(closes[i - span : i + 1 : step], period=PERIOD)
        if band:
            z[i] = band["z"]
            ma[i] = band["ma"]
    return z, ma


def _daily_levels(b: Bars) -> None:
    """Каналы и средние по дневкам из часовых свечей; день i видит только дни до себя."""
    local: list[int] = []
    d_hi: list[float] = []
    d_lo: list[float] = []
    d_close: list[float] = []
    for i, t in enumerate(b.ts):
        if not local or b.ts[i - 1].date() != t.date():
            d_hi.append(b.h[i])
            d_lo.append(b.l[i])
            d_close.append(b.c[i])
        else:
            d_hi[-1] = max(d_hi[-1], b.h[i])
            d_lo[-1] = min(d_lo[-1], b.l[i])
            d_close[-1] = b.c[i]
        local.append(len(d_close) - 1)

    def prior(values: list[float], n: int, fn) -> list[float | None]:
        return [fn(values[k - n : k]) if k >= n else None for k in range(len(values))]

    for n in CHANNEL_DAYS:
        hi = prior(d_hi, n, max)
        lo = prior(d_lo, n, min)
        b.ch_hi[n] = [hi[k] for k in local]
        b.ch_lo[n] = [lo[k] for k in local]
    for n in MA_DAYS:
        ma = prior(d_close, n, lambda xs: sum(xs) / len(xs))
        b.dma[n] = [ma[k] for k in local]


def build_universe(
    raw: dict[str, list[list]],
    lots: dict[str, int],
    divs: dict[str, list[list]],
    imoex: tuple[list[datetime], list[bool]],
    *,
    sim_start: date,
    capital: float,
    slip_pct: float,
    fund_pct: float,
) -> Universe:
    all_dates = sorted({_parse_ts(r[6]).date() for rows in raw.values() for r in rows})
    day_idx = {d: i for i, d in enumerate(all_dates)}
    risk_ts, risk_flags = imoex
    bars_list: list[Bars] = []
    for ticker, rows in raw.items():
        ts = [_parse_ts(r[6]) for r in rows]
        risk = []
        for t in ts:
            k = bisect_right(risk_ts, t) - 1
            risk.append(risk_flags[k] if k >= 0 else False)
        b = Bars(
            ticker=ticker,
            lot=lots.get(ticker, 1),
            ts=ts,
            day=[day_idx[t.date()] for t in ts],
            o=[float(r[0]) for r in rows],
            h=[float(r[2]) for r in rows],
            l=[float(r[3]) for r in rows],
            c=[float(r[1]) for r in rows],
            risk=risk,
        )
        for tf, step in TF_STEP.items():
            b.z[tf], b.ma[tf] = _bands(b.c, step)
        _daily_levels(b)
        for cutoff, amount, strict in divs.get(ticker, []):
            d = date.fromisoformat(cutoff)
            ex = bisect_right(all_dates, d) if strict else bisect_left(all_dates, d)
            if ex >= len(all_dates):
                continue
            b.div_on_day[ex] = b.div_on_day.get(ex, 0.0) + float(amount)
            b.blocked.update(range(ex, ex + DIV_BLACKOUT_DAYS))
        bars_list.append(b)
    grouped: dict[datetime, list[tuple[int, int]]] = {}
    for ti, b in enumerate(bars_list):
        for i, t in enumerate(b.ts):
            grouped.setdefault(t, []).append((ti, i))
    events = sorted(grouped.items())
    sim_from = bisect_left(all_dates, sim_start)
    return Universe(bars_list, all_dates, events, sim_from, capital, slip_pct, fund_pct)


# ---------- симуляция ----------


@dataclass(frozen=True)
class Params:
    tariff: str
    slots: int
    kind: str = "dip"
    stop_pct: float = 3.0
    # dip
    tf: str = "4h"
    entry_z: float = -2.0
    take_pct: float | None = None
    hold_days: int = 10
    # trend
    entry_n: int = 20
    ma_days: int = 0
    trail_pct: float | None = None
    exit_n: int = 0


@dataclass
class Pos:
    qty: int
    cost: float
    day: int
    stop: float
    take: float | None
    peak: float
    divs: float = 0.0


@dataclass
class Result:
    params: Params
    net: float
    per_day: float
    trades: int
    wins: int
    pnl_trades: float
    fees: float
    fund: float
    divs: float
    dd_rub: float
    dd_pct: float
    exposure: float
    skipped: int
    by_ticker: dict[str, list[float]]


_DATA: Universe | None = None


def trade_days(u: Universe, span: tuple[float, float]) -> tuple[int, int, float]:
    n = len(u.dates) - u.sim_from
    lo = u.sim_from + int(n * span[0])
    hi = max(lo + 1, u.sim_from + int(n * span[1]))
    cal = (u.dates[hi - 1] - u.dates[lo]).days + 1
    return lo, hi, cal * TRADE_DAYS_PER_YEAR / 365


def fund_only_per_day(u: Universe, span: tuple[float, float] = (0.0, 1.0)) -> float:
    _lo, _hi, tdays = trade_days(u, span)
    cal = tdays * 365 / TRADE_DAYS_PER_YEAR
    return u.capital * u.fund_pct / 100 * cal / 365 * (1 - TAX) / tdays


def hold_all_per_day(u: Universe, fee_pct: float) -> float:
    """Равными долями в начале теста и держать до конца, с дивидендами."""
    lo, hi, tdays = trade_days(u, (0.0, 1.0))
    slot = u.capital / len(u.bars)
    final = 0.0
    for b in u.bars:
        idx = [i for i, d in enumerate(b.day) if lo <= d < hi]
        if not idx:
            final += slot
            continue
        first, last = b.c[idx[0]], b.c[idx[-1]]
        div = sum(v for d, v in b.div_on_day.items() if lo < d < hi) * (1 - TAX)
        shares = slot * (1 - fee_pct / 100) / first
        final += shares * (last + div) * (1 - fee_pct / 100)
    profit = final - u.capital
    return (profit - max(profit, 0.0) * TAX) / tdays


def _exit_price(p: Params, ps: Pos, b: Bars, i: int, day: int, fee: float, slip: float) -> tuple[float | None, str]:
    o, h, lo_, c = b.o[i], b.h[i], b.l[i], b.c[i]
    if o <= ps.stop:
        return o * (1 - slip), "стоп"
    if ps.take is not None and o >= ps.take:
        return o, "тейк"
    if lo_ <= ps.stop:
        return ps.stop * (1 - slip), "стоп"
    if p.kind == "trend":
        floor = b.ch_lo[p.exit_n][i] if p.exit_n else None
        if floor is not None and c < floor:
            return c * (1 - slip), "канал"
        return None, ""
    if ps.take is not None and h >= ps.take:
        return ps.take, "тейк"
    ma = b.ma[p.tf][i]
    if ps.take is None and ma is not None and c >= ma and c * (1 - slip) * (1 - fee) * ps.qty > ps.cost:
        return c * (1 - slip), "средняя"
    if day - ps.day >= p.hold_days:
        return c * (1 - slip), "время"
    return None, ""


def _entry_score(p: Params, b: Bars, i: int) -> float | None:
    """Меньше — сильнее сигнал. None — входа нет."""
    c = b.c[i]
    if p.kind == "trend":
        top = b.ch_hi[p.entry_n][i]
        if top is None or c <= top:
            return None
        if p.ma_days:
            ma = b.dma[p.ma_days][i]
            if ma is None or c <= ma:
                return None
        return -(c / top)
    z = b.z[p.tf][i]
    if z is None or z > p.entry_z:
        return None
    return z


def simulate(p: Params, span: tuple[float, float] = (0.0, 1.0)) -> Result:
    u = _DATA
    assert u is not None
    fee_pct, monthly = TARIFFS[p.tariff]
    fee = fee_pct / 100
    slip = u.slip_pct / 100
    d_lo, d_hi, tdays = trade_days(u, span)

    cash = u.capital
    pos: dict[int, Pos] = {}
    last_px: dict[int, float] = {}
    cooldown: dict[int, int] = {}
    paid_months: set[tuple[int, int]] = set()
    by_ticker: dict[str, list[float]] = {}
    fees = fund = divs = pnl_trades = 0.0
    trades = wins = skipped = 0
    peak_eq = u.capital
    dd_rub = dd_pct = 0.0
    exp_sum = 0.0
    exp_n = 0
    cur_day: int | None = None

    def equity() -> float:
        return cash + sum(ps.qty * last_px[k] for k, ps in pos.items())

    def charge_month(t: datetime) -> None:
        nonlocal cash, fees
        key = (t.year, t.month)
        if monthly and key not in paid_months:
            paid_months.add(key)
            cash -= monthly
            fees += monthly

    for ts, group in u.events:
        day = u.bars[group[0][0]].day[group[0][1]]
        if day < d_lo:
            continue
        if day >= d_hi:
            break
        if day != cur_day:
            if cur_day is not None:
                eq = equity()
                peak_eq = max(peak_eq, eq)
                dd_rub = max(dd_rub, peak_eq - eq)
                dd_pct = max(dd_pct, (peak_eq - eq) / peak_eq * 100 if peak_eq > 0 else 0.0)
                exp_sum += (eq - cash) / eq if eq > 0 else 0.0
                exp_n += 1
                gap = (u.dates[day] - u.dates[cur_day]).days
                earned = max(cash, 0.0) * u.fund_pct / 100 * gap / 365
                cash += earned
                fund += earned
            cur_day = day
            for ti, ps in pos.items():
                amount = u.bars[ti].div_on_day.get(day)
                if amount:
                    got = amount * ps.qty * (1 - TAX)
                    cash += got
                    ps.divs += got
                    divs += got

        for ti, i in group:
            ps = pos.get(ti)
            if ps is None:
                continue
            b = u.bars[ti]
            px, reason = _exit_price(p, ps, b, i, day, fee, slip)
            if px is None:
                if p.kind == "trend" and p.trail_pct:
                    ps.peak = max(ps.peak, b.c[i])
                    ps.stop = max(ps.stop, ps.peak * (1 - p.trail_pct / 100))
                continue
            proceeds = px * ps.qty
            fee_rub = proceeds * fee
            cash += proceeds - fee_rub
            fees += fee_rub
            pnl = proceeds - fee_rub - ps.cost + ps.divs
            pnl_trades += pnl
            trades += 1
            wins += pnl > 0
            row = by_ticker.setdefault(b.ticker, [0, 0.0])
            row[0] += 1
            row[1] += pnl
            cooldown[ti] = day + 1 if reason == "стоп" else (day if p.kind == "trend" else -1)
            del pos[ti]
            charge_month(ts)

        cands: list[tuple[float, int, int]] = []
        for ti, i in group:
            b = u.bars[ti]
            last_px[ti] = b.c[i]
            if ti in pos:
                continue
            t = b.ts[i]
            if t.weekday() >= 5 or not (ENTRY_HOUR_FROM <= t.hour < ENTRY_HOUR_TILL):
                continue
            if b.risk[i] or day in b.blocked or cooldown.get(ti, -1) >= day:
                continue
            score = _entry_score(p, b, i)
            if score is not None:
                cands.append((score, ti, i))
        cands.sort()
        for _score, ti, i in cands:
            if len(pos) >= p.slots:
                break
            b = u.bars[ti]
            budget = min(equity() / p.slots, cash)
            px = b.c[i] * (1 + slip)
            lots = int(budget // (px * b.lot * (1 + fee)))
            if lots <= 0:
                skipped += 1
                continue
            qty = lots * b.lot
            value = px * qty
            fee_rub = value * fee
            cash -= value + fee_rub
            fees += fee_rub
            pos[ti] = Pos(
                qty=qty,
                cost=value + fee_rub,
                day=day,
                stop=px * (1 - p.stop_pct / 100),
                take=px * (1 + p.take_pct / 100) if p.kind == "dip" and p.take_pct else None,
                peak=px,
            )
            charge_month(ts)

    final = cash
    for ti, ps in pos.items():
        proceeds = last_px[ti] * (1 - slip) * ps.qty
        final += proceeds * (1 - fee)
        fees += proceeds * fee
    profit = final - u.capital
    taxable = profit - divs
    net = profit - max(taxable, 0.0) * TAX
    return Result(
        params=p,
        net=net,
        per_day=net / tdays,
        trades=trades,
        wins=wins,
        pnl_trades=pnl_trades,
        fees=fees,
        fund=fund,
        divs=divs,
        dd_rub=dd_rub,
        dd_pct=dd_pct,
        exposure=100 * exp_sum / max(exp_n, 1),
        skipped=skipped,
        by_ticker=by_ticker,
    )


def _simulate_half(args: tuple[Params, tuple[float, float]]) -> Result:
    return simulate(args[0], args[1])


def dip_grid() -> list[Params]:
    return [
        Params(tariff, slots, "dip", stop, tf=tf, entry_z=z, take_pct=take, hold_days=hold_d)
        for tariff, slots, tf, z, take, stop, hold_d in itertools.product(
            TARIFFS,
            (2, 3, 4),
            ("1h", "4h"),
            (-1.5, -2.0, -2.5),
            (1.0, 1.5, 2.5, 4.0, None),
            (2.0, 3.0, 5.0),
            (3, 10),
        )
    ]


def trend_grid() -> list[Params]:
    exits = ((5.0, 0), (8.0, 0), (12.0, 0), (None, 10), (None, 20))
    return [
        Params(tariff, slots, "trend", stop, entry_n=n, ma_days=ma, trail_pct=trail, exit_n=exit_n)
        for tariff, slots, n, ma, (trail, exit_n), stop in itertools.product(
            TARIFFS,
            (2, 3, 4),
            CHANNEL_DAYS,
            (0, *MA_DAYS),
            exits,
            (5.0, 8.0),
        )
    ]


# ---------- вывод ----------


def _name(p: Params) -> str:
    head = f"{p.tariff:<8} {p.slots} сл."
    if p.kind == "trend":
        filt = f" над MA{p.ma_days}" if p.ma_days else ""
        out = f"трейл {p.trail_pct:g}%" if p.trail_pct else f"минимум {p.exit_n}д"
        return f"{head} тренд: пробой {p.entry_n}д{filt}, выход {out}, стоп {p.stop_pct:g}%"
    take = f"+{p.take_pct:g}%" if p.take_pct else "к средней"
    return f"{head} просадка: {p.tf} z≤{p.entry_z:g}, выход {take}, стоп {p.stop_pct:g}% ≤{p.hold_days}д"


def _row(r: Result, target: float) -> str:
    mark = "✓" if r.per_day >= target else " "
    win = 100 * r.wins / max(r.trades, 1)
    avg = r.pnl_trades / max(r.trades, 1)
    return (
        f"{mark} {_name(r.params):<70} {r.per_day:+6.1f} ₽/день  итог {r.net:+8.0f} ₽  "
        f"сделок {r.trades:4d}  win {win:5.1f}%  ср. {avg:+6.1f} ₽  "
        f"комис. {r.fees:6.0f} ₽  просадка {r.dd_rub:6.0f} ₽ ({r.dd_pct:4.1f}%)  в рынке {r.exposure:3.0f}%"
    )


def _report_kind(
    title: str,
    grid: list[Params],
    pool: ProcessPoolExecutor,
    *,
    target: float,
    top: int,
) -> tuple[Result, float]:
    started = time.time()
    results = list(pool.map(simulate, grid, chunksize=8))
    results.sort(key=lambda r: r.per_day, reverse=True)
    print(f"\n=== {title}: {len(grid)} вариантов за {time.time() - started:.0f} с. Лучшие {top} по ₽/день:")
    for r in results[:top]:
        print(_row(r, target))

    shortlist = [r.params for r in results[:30]]
    halves = list(
        pool.map(
            _simulate_half,
            [(p, (0.0, 0.5)) for p in shortlist] + [(p, (0.5, 1.0)) for p in shortlist],
        )
    )
    first, second = halves[: len(shortlist)], halves[len(shortlist) :]
    robust = sorted(zip(shortlist, first, second), key=lambda x: min(x[1].per_day, x[2].per_day), reverse=True)
    print("Устойчивость (из 30 лучших, по худшей половине истории):")
    for p, h1, h2 in robust[:6]:
        mark = "✓" if min(h1.per_day, h2.per_day) >= target else " "
        print(
            f"{mark} {_name(p):<70} 1-я половина {h1.per_day:+6.1f} ₽/день (win {100 * h1.wins / max(h1.trades, 1):4.1f}%)  "
            f"2-я {h2.per_day:+6.1f} ₽/день (win {100 * h2.wins / max(h2.trades, 1):4.1f}%)"
        )
    best_p, b1, b2 = robust[0]
    best = next(r for r in results if r.params == best_p)
    print(f"Устойчивый лучший: {_name(best_p)}")
    print("  " + _row(best, target).strip())
    for ticker, (count, pnl) in sorted(best.by_ticker.items(), key=lambda x: x[1][1], reverse=True):
        print(f"    {ticker:<5} сделок {int(count):3d}  результат {pnl:+8.0f} ₽")
    print(
        f"    фонд на свободном кэше {best.fund:+.0f} ₽, дивиденды {best.divs:+.0f} ₽, "
        f"комиссии {best.fees:.0f} ₽, не хватило на лот {best.skipped} раз"
    )
    return best, min(b1.per_day, b2.per_day)


def main() -> int:
    global _DATA
    parser = argparse.ArgumentParser(description="Бэктест стратегий фондового стола на MOEX.")
    parser.add_argument("--capital", type=float, default=30000.0, help="счёт, ₽")
    parser.add_argument("--target", type=float, default=30.0, help="цель, чистых ₽ в торговый день")
    parser.add_argument("--days", type=int, default=730, help="длина теста, календарных дней")
    parser.add_argument("--fund", type=float, default=14.0, help="доходность фонда ликвидности, %% годовых")
    parser.add_argument("--slip", type=float, default=0.03, help="спред/проскальзывание, %% за сторону")
    parser.add_argument("--kind", choices=("all", "dip", "trend"), default="all")
    parser.add_argument("--tickers", default=",".join(TICKERS))
    parser.add_argument("--no-divs", action="store_true", help="не брать дивиденды из Т-Инвест")
    parser.add_argument("--refresh", action="store_true", help="перекачать свечи мимо кэша")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    till = date.today()
    start = till - timedelta(days=args.days)
    fetch_from = start - timedelta(days=WARMUP_DAYS)

    lots = fetch_lots(tickers)
    raw: dict[str, list[list]] = {}
    for ticker in tickers:
        try:
            rows = fetch_candles(f"shares/boards/TQBR/securities/{ticker}", fetch_from, till, args.refresh)
        except Exception as exc:
            print(f"{ticker}: нет свечей ({exc})")
            continue
        if len({r[6][:10] for r in rows}) < 60:
            print(f"{ticker}: мало истории ({len(rows)} ч)")
            continue
        raw[ticker] = rows
    if not raw:
        print("Нет данных.")
        return 1
    imoex_rows = fetch_candles("index/securities/IMOEX", fetch_from, till, args.refresh)
    divs: dict[str, list[list]] = {}
    if not args.no_divs:
        try:
            divs = fetch_dividends(sorted(raw), fetch_from, till, args.refresh)
        except Exception as exc:
            print(f"Дивиденды не загрузились ({exc}) — считаю без них, гэпы отсечек будут ловушками.\n")

    _DATA = build_universe(
        raw,
        lots,
        divs,
        risk_by_ts(imoex_rows),
        sim_start=start,
        capital=args.capital,
        slip_pct=args.slip,
        fund_pct=args.fund,
    )
    u = _DATA
    lo, _hi, tdays = trade_days(u, (0.0, 1.0))
    imoex_in = [r for r in imoex_rows if r[6][:10] >= u.dates[lo].isoformat()]
    imoex_chg = (float(imoex_in[-1][1]) / float(imoex_in[0][1]) - 1) * 100 if imoex_in else 0.0
    fund_day = fund_only_per_day(u)
    hold_day = hold_all_per_day(u, TARIFFS["инвестор"][0])
    print(
        f"Бумаг: {len(u.bars)} ({', '.join(b.ticker for b in u.bars)}), {u.dates[lo]} — {u.dates[-1]}, "
        f"≈{tdays:.0f} торговых дней; дивидендных отсечек: {sum(len(b.div_on_day) for b in u.bars)}"
    )
    print(
        f"Счёт {args.capital:,.0f} ₽, цель {args.target:g} ₽/день чистыми; фонд {args.fund:g}% годовых, "
        f"спред {args.slip:g}% за сторону, налог {TAX * 100:.0f}%; IMOEX за период {imoex_chg:+.1f}%".replace(",", " ")
    )
    print(f"  всё в фонде ликвидности:          {fund_day:+6.1f} ₽/день")
    print(f"  купить все 12 поровну и держать:  {hold_day:+6.1f} ₽/день (с дивидендами)")

    ctx = mp.get_context("fork")
    summary: list[tuple[str, Result, float]] = []
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as pool:
        if args.kind in ("all", "dip"):
            best, worst = _report_kind("Просадка", dip_grid(), pool, target=args.target, top=args.top)
            summary.append(("просадка", best, worst))
        if args.kind in ("all", "trend"):
            best, worst = _report_kind("Тренд", trend_grid(), pool, target=args.target, top=args.top)
            summary.append(("тренд", best, worst))

    print("\nИтог:")
    print(f"  всё в фонде:         {fund_day:+6.1f} ₽/день")
    print(f"  купить и держать:    {hold_day:+6.1f} ₽/день")
    for kind, best, worst in summary:
        print(f"  {kind + ':':<20} {best.per_day:+6.1f} ₽/день, в худшую половину {worst:+.1f}  ({_name(best.params)})")
    for kind, best, worst in summary:
        if worst >= args.target:
            verdict = f"цель {args.target:g} ₽/день держится в обеих половинах — можно в песочницу"
        elif worst > fund_day:
            verdict = "цель не держится, но фонд обгоняет стабильно"
        else:
            verdict = "стабильно обогнать фонд не получилось"
        print(f"  {kind}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
