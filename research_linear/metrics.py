"""Метрики ATR / ER / wick / corr для research."""

from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from .bybit_public import fnum


def median(xs: Sequence[float]) -> float:
    vals = [x for x in xs if not (isinstance(x, float) and math.isnan(x))]
    if not vals:
        return float("nan")
    return float(statistics.median(vals))


def mean(xs: Sequence[float]) -> float:
    vals = [x for x in xs if not (isinstance(x, float) and math.isnan(x))]
    if not vals:
        return float("nan")
    return float(statistics.fmean(vals))


def corr(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return float("nan")
    aa = list(a)[-n:]
    bb = list(b)[-n:]
    ma = statistics.fmean(aa)
    mb = statistics.fmean(bb)
    num = sum((x - ma) * (y - mb) for x, y in zip(aa, bb))
    da = math.sqrt(sum((x - ma) ** 2 for x in aa))
    db = math.sqrt(sum((y - mb) ** 2 for y in bb))
    if da < 1e-18 or db < 1e-18:
        return float("nan")
    return num / (da * db)


def closes(rows: list[list[Any]]) -> list[float]:
    return [fnum(r[4]) for r in rows]


def atr_pct_series(rows: list[list[Any]], period: int = 14) -> list[float]:
    if len(rows) < period + 2:
        return []
    trs: list[float] = []
    prev = fnum(rows[0][4])
    for row in rows[1:]:
        h, low, c = fnum(row[2]), fnum(row[3]), fnum(row[4])
        tr = max(h - low, abs(h - prev), abs(low - prev))
        if prev > 0:
            trs.append(tr / prev * 100.0)
        prev = c
    if len(trs) < period:
        return []
    out: list[float] = []
    atr = sum(trs[:period]) / period
    out.append(atr)
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        out.append(atr)
    return out


def atr_pct_median(rows: list[list[Any]], period: int = 14) -> float:
    return median(atr_pct_series(rows, period))


def efficiency_ratio(closes_: list[float], bars: int) -> float:
    if len(closes_) < bars + 5:
        return float("nan")
    ers: list[float] = []
    for i in range(bars, len(closes_)):
        net = abs(closes_[i] - closes_[i - bars])
        path = sum(abs(closes_[j] - closes_[j - 1]) for j in range(i - bars + 1, i + 1))
        if path < 1e-18:
            continue
        ers.append(net / path)
    return mean(ers)


def wick_share_mean(rows: list[list[Any]]) -> float:
    vals: list[float] = []
    for row in rows:
        o, h, low, c = fnum(row[1]), fnum(row[2]), fnum(row[3]), fnum(row[4])
        rng = h - low
        if rng <= 0:
            continue
        vals.append((rng - abs(c - o)) / rng)
    return mean(vals)


def hourly_returns(rows_1h: list[list[Any]]) -> list[float]:
    c = closes(rows_1h)
    out: list[float] = []
    for i in range(1, len(c)):
        if c[i - 1] <= 0:
            continue
        out.append(c[i] / c[i - 1] - 1.0)
    return out


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out
