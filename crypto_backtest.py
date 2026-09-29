#!/usr/bin/env python3
"""Бэктест рукава альтов на часовых свечах Bybit (спот, только long).

Сравнивает Боллинджер (вход по z 4h, выход у средней, стоп от входа) с прежним
mean-reversion по суточному изменению и с «купил и держи». Комиссия — за сторону.
risk-off по BTC как в столе: entry — не входим, exit — ещё и выходим в кэш.

    .venv/bin/python crypto_backtest.py            # сетка + лучшие
    .venv/bin/python crypto_backtest.py --pages 4  # короче история
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from skills.crypto.desk_policy import (
    ALT_BB_ENTRY_Z,
    ALT_EXIT_MIN_GAIN_PCT,
    ALT_STOP_LOSS_PCT,
    CHURN_COOLDOWN_HOURS,
    DESK_ALT_SLEEVE,
    TRAIL_ALT_PCT,
    TRAIL_ARM_PCT,
    WATCH_TP_ALT_DAY_PCT,
    is_risk_off,
)
from skills.crypto.indicators import bollinger

API = "https://api.bybit.com/v5/market/kline"
CACHE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "jarvis_bt"
PERIOD = 20
# Прежний рукав (до Боллинджера): докуп на суточной просадке в окне 7д.
LEGACY_DIP_DAY_PCT = -4.0
LEGACY_CHG7_FLOOR = -18.0
LEGACY_CHG7_CEIL = 8.0


def fetch_hourly(ticker: str, pages: int) -> list[tuple[int, float]]:
    """(время, закрытие) 1 ч от старых к новым (до pages×1000 свечей)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{ticker}_60_{pages}_ts.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
        return [tuple(x) for x in json.loads(cache.read_text())]
    rows: list[list[str]] = []
    end: int | None = None
    for _ in range(pages):
        params = {"category": "spot", "symbol": f"{ticker}USDT", "interval": "60", "limit": "1000"}
        if end:
            params["end"] = str(end)
        resp = requests.get(API, params=params, timeout=15)
        resp.raise_for_status()
        chunk = (resp.json().get("result") or {}).get("list") or []
        if not chunk:
            break
        rows.extend(chunk)
        end = int(chunk[-1][0]) - 1
        if len(chunk) < 1000:
            break
        time.sleep(0.15)
    seen: dict[int, float] = {int(r[0]): float(r[4]) for r in rows}
    out = [(t, seen[t]) for t in sorted(seen)]
    cache.write_text(json.dumps(out))
    return out


def risk_off_by_ts(btc: list[tuple[int, float]]) -> dict[int, bool]:
    closes = [c for _, c in btc]
    out: dict[int, bool] = {}
    for i, (ts, price) in enumerate(btc):
        if i < 24 * 7:
            continue
        day = (price / closes[i - 24] - 1) * 100
        week = (price / closes[i - 24 * 7] - 1) * 100
        out[ts] = is_risk_off(btc_chg_7=week, btc_chg_day=day)
    return out


@dataclass
class Result:
    ret_pct: float
    trades: int
    wins: int
    max_dd_pct: float
    exposure: float


@dataclass
class Series:
    ticker: str
    closes: list[float]
    risk: list[bool]
    b4: list[dict[str, float] | None]


def _series_4h(closes: list[float], i: int) -> list[float]:
    """4-часовые закрытия, заканчивающиеся на баре i (текущая свеча = текущая цена)."""
    return [closes[j] for j in range(i - 4 * (PERIOD - 1), i + 1, 4) if j >= 0]


def build(ticker: str, rows: list[tuple[int, float]], risk_map: dict[int, bool]) -> Series:
    closes = [c for _, c in rows]
    risk = [risk_map.get(ts, False) for ts, _ in rows]
    b4: list[dict[str, float] | None] = [None] * len(closes)
    for i in range(4 * PERIOD, len(closes)):
        b4[i] = bollinger(_series_4h(closes, i), period=PERIOD)
    return Series(ticker, closes, risk, b4)


class _Book:
    def __init__(self, fee_pct: float) -> None:
        self.fee = fee_pct / 100
        self.equity = 1.0
        self.peak_eq = 1.0
        self.max_dd = 0.0
        self.entry = 0.0
        self.in_pos = False
        self.trades = self.wins = self.bars_in = 0

    def buy(self, price: float) -> None:
        self.equity *= 1 - self.fee
        self.entry = price
        self.in_pos = True

    def mark(self, price: float) -> float:
        self.bars_in += 1
        gain = price / self.entry - 1
        m = self.equity * (1 + gain) * (1 - self.fee)
        self.peak_eq = max(self.peak_eq, m)
        self.max_dd = max(self.max_dd, 1 - m / self.peak_eq)
        return gain

    def sell(self, price: float) -> None:
        gain = price / self.entry - 1
        self.equity = self.equity * (1 + gain) * (1 - self.fee)
        self.trades += 1
        self.wins += gain > 2 * self.fee
        self.in_pos = False

    def result(self, last: float, bars: int) -> Result:
        if self.in_pos:
            self.sell(last)
        return Result((self.equity - 1) * 100, self.trades, self.wins, self.max_dd * 100, self.bars_in / max(bars, 1))


def _span(n: int, start: int, span: tuple[float, float]) -> range:
    lo = max(start, int(n * span[0]))
    return range(lo, int(n * span[1]))


def run_bollinger(
    s: Series,
    *,
    entry_z: float,
    min_gain_pct: float,
    stop_pct: float | None,
    riskoff: str,
    fee_pct: float,
    span: tuple[float, float] = (0.0, 1.0),
) -> Result:
    book = _Book(fee_pct)
    cooldown_until = -1
    bars = _span(len(s.closes), 4 * PERIOD, span)
    last = s.closes[bars.start]
    for i in bars:
        price = last = s.closes[i]
        b4 = s.b4[i]
        if not b4:
            continue
        if book.in_pos:
            gain = book.mark(price)
            hit_exit = price >= b4["ma"] and gain * 100 >= min_gain_pct
            hit_stop = stop_pct is not None and gain * 100 <= -stop_pct
            hit_risk = riskoff == "exit" and s.risk[i]
            if hit_exit or hit_stop or hit_risk:
                book.sell(price)
                if hit_stop:
                    cooldown_until = i + int(CHURN_COOLDOWN_HOURS)
            continue
        if i < cooldown_until or (riskoff != "off" and s.risk[i]):
            continue
        if b4["z"] <= entry_z:
            book.buy(price)
    return book.result(last, len(bars))


def run_current(s: Series, *, fee_pct: float, riskoff: str = "off") -> Result:
    """Прежний рукав: вход на суточной просадке, выход по суточному росту или трейлу."""
    book = _Book(fee_pct)
    peak = 0.0
    armed = False
    cooldown_until = -1
    closes = s.closes
    bars = range(24 * 7, len(closes))
    for i in bars:
        price = closes[i]
        day = (price / closes[i - 24] - 1) * 100
        week = (price / closes[i - 24 * 7] - 1) * 100
        if book.in_pos:
            gain = book.mark(price)
            peak = max(peak, price)
            armed = armed or gain * 100 >= TRAIL_ARM_PCT
            trail_hit = armed and price <= peak * (1 - TRAIL_ALT_PCT / 100)
            if day >= WATCH_TP_ALT_DAY_PCT or trail_hit or (riskoff == "exit" and s.risk[i]):
                book.sell(price)
                cooldown_until = i + int(CHURN_COOLDOWN_HOURS)
            continue
        if i < cooldown_until or (riskoff != "off" and s.risk[i]):
            continue
        if day <= LEGACY_DIP_DAY_PCT and LEGACY_CHG7_FLOOR <= week <= LEGACY_CHG7_CEIL:
            book.buy(price)
            peak = price
            armed = False
    return book.result(closes[-1], len(bars))


def _avg(results: list[Result]) -> dict[str, float]:
    n = len(results) or 1
    trades = sum(r.trades for r in results)
    return {
        "ret": sum(r.ret_pct for r in results) / n,
        "worst": min(r.ret_pct for r in results),
        "trades": trades / n,
        "win": 100 * sum(r.wins for r in results) / max(trades, 1),
        "dd": sum(r.max_dd_pct for r in results) / n,
        "exp": 100 * sum(r.exposure for r in results) / n,
    }


def _row(name: str, s: dict[str, float]) -> str:
    return (
        f"{name:<42} ret {s['ret']:+7.1f}%  worst {s['worst']:+7.1f}%  "
        f"trades {s['trades']:5.1f}  win {s['win']:5.1f}%  dd {s['dd']:5.1f}%  in {s['exp']:4.0f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=8, help="по 1000 часовых свечей")
    parser.add_argument("--fee", type=float, default=0.18, help="комиссия %% за сторону (taker Bybit)")
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    btc = fetch_hourly("BTC", args.pages)
    risk_map = risk_off_by_ts(btc)
    series: list[Series] = []
    for ticker in sorted(DESK_ALT_SLEEVE):
        try:
            rows = fetch_hourly(ticker, args.pages)
        except Exception as exc:
            print(f"{ticker}: нет свечей ({exc})")
            continue
        if len(rows) < 24 * 30:
            print(f"{ticker}: мало истории ({len(rows)} ч)")
            continue
        series.append(build(ticker, rows, risk_map))
    if not series:
        print("Нет данных.")
        return 1
    days = min(len(s.closes) for s in series) / 24
    risk_share = 100 * sum(risk_map.values()) / max(len(risk_map), 1)
    btc_ret = (btc[-1][1] / btc[0][1] - 1) * 100
    print(
        f"Монет: {len(series)}, истории ≥ {days:.0f} дн., комиссия {args.fee}% за сторону; "
        f"BTC {btc_ret:+.1f}%, risk-off {risk_share:.0f}% времени\n"
    )

    hold = [Result((s.closes[-1] / s.closes[0] - 1) * 100, 1, int(s.closes[-1] > s.closes[0]), 0.0, 1.0) for s in series]
    print(_row("купил и держи", _avg(hold)))
    for mode in ("off", "entry", "exit"):
        res = [run_current(s, fee_pct=args.fee, riskoff=mode) for s in series]
        print(_row(f"прежний рукав, risk-off {mode}", _avg(res)))
    live = dict(
        entry_z=ALT_BB_ENTRY_Z,
        min_gain_pct=ALT_EXIT_MIN_GAIN_PCT,
        stop_pct=ALT_STOP_LOSS_PCT,
    )
    for mode in ("entry", "exit"):
        res = [run_bollinger(s, fee_pct=args.fee, riskoff=mode, **live) for s in series]
        print(_row(f"стол сейчас (BB), risk-off {mode}", _avg(res)))
    print()

    grid = itertools.product(
        (-2.0, -2.5, -3.0),
        (1.0, 2.0, 3.0),
        (None, 10.0, 12.0, 15.0),
        ("off", "entry", "exit"),
    )
    scored: list[tuple[float, dict, dict[str, float]]] = []
    for entry_z, min_gain, stop, riskoff in grid:
        params = dict(entry_z=entry_z, min_gain_pct=min_gain, stop_pct=stop, riskoff=riskoff)
        s = _avg([run_bollinger(x, fee_pct=args.fee, **params) for x in series])
        scored.append((s["ret"] - 0.5 * s["dd"], params, s))
    scored.sort(key=lambda x: x[0], reverse=True)

    def _name(p: dict) -> str:
        return f"BB z≤{p['entry_z']} ≥{p['min_gain_pct']}% стоп {p['stop_pct'] or '—'} r-o {p['riskoff']}"

    print(f"Лучшие {args.top} (ret − 0.5·dd):")
    for _, p, s in scored[: args.top]:
        print(_row(_name(p), s))

    print("\nУстойчивость лучших 5 по половинам истории:")
    for _, p, _s in scored[:5]:
        h1 = _avg([run_bollinger(x, fee_pct=args.fee, span=(0.0, 0.5), **p) for x in series])
        h2 = _avg([run_bollinger(x, fee_pct=args.fee, span=(0.5, 1.0), **p) for x in series])
        print(f"{_name(p):<42} 1-я {h1['ret']:+6.1f}% (dd {h1['dd']:4.1f})  2-я {h2['ret']:+6.1f}% (dd {h2['dd']:4.1f})")

    best = scored[0][1]
    print("\nЛучший по монетам:")
    for x in series:
        r = run_bollinger(x, fee_pct=args.fee, **best)
        h = (x.closes[-1] / x.closes[0] - 1) * 100
        print(f"  {x.ticker:<5} {r.ret_pct:+7.1f}%  (держать {h:+7.1f}%)  сделок {r.trades:3d}  dd {r.max_dd_pct:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
