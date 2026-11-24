"""Единое ядро фичей/сигналов: backtest == live (stateless поверх OHLC DataFrame).

Сигнал считается только на закрытом 15m-баре; 1h/4h — только полностью закрытые
(join asof по close_ts предыдущего бара).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

Side = Literal["long", "short"]


@dataclass(frozen=True)
class StrategyParams:
    h1a_4h: bool = True
    h1b_er4h: bool = True
    er4h_median_mode: str = "rolling_4320"  # expanding | rolling_4320 | rolling_2880
    h2_impulse: bool = True
    h2_min_sl_atr: float = 1.5
    btc_filter: bool = True
    tp_r: float = 2.5
    # entry mid of confirm candle (не H3)
    use_h3_entry: bool = False


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    bar_ts: int  # open time of closed 15m bar that confirmed
    limit_px: float
    sl: float
    risk_px: float
    tp: float
    atr15: float
    er_4h: float
    confirm_close: float


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out


def _atr_abs(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    n = len(closes)
    out = [float("nan")] * n
    if n < period + 1:
        return out
    trs = []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:period]) / period
    out[period] = atr
    for j in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[j]) / period
        out[j + 1] = atr
    return out


def _join_htf(m15: pd.DataFrame, htf: pd.DataFrame, prefix: str, period_ms: int) -> pd.DataFrame:
    h = htf.copy()
    closes = h["close"].astype(float).tolist()
    highs = h["high"].astype(float).tolist()
    lows = h["low"].astype(float).tolist()
    h["ema20"] = _ema(closes, 20)
    h["ema50"] = _ema(closes, 50)
    h["atr"] = _atr_abs(highs, lows, closes)
    h["close_ts"] = h["ts"].astype("int64") + period_ms
    right = h[["close_ts", "ema20", "ema50", "atr", "open", "high", "low", "close"]].rename(
        columns={
            "ema20": f"ema20_{prefix}",
            "ema50": f"ema50_{prefix}",
            "atr": f"atr_{prefix}",
            "open": f"{prefix}_open",
            "high": f"{prefix}_high",
            "low": f"{prefix}_low",
            "close": f"{prefix}_close",
        }
    )
    out = pd.merge_asof(
        m15.sort_values("ts"),
        right.sort_values("close_ts"),
        left_on="ts",
        right_on="close_ts",
        direction="backward",
    )
    if "close_ts" in out.columns:
        out = out.drop(columns=["close_ts"], errors="ignore")
    return out


def build_features(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
) -> pd.DataFrame:
    """Чистая функция: OHLC 15m/1h/4h → feature frame (oldest→newest, ts=open ms)."""
    need = {"ts", "open", "high", "low", "close"}
    for name, df in (("15m", m15), ("1h", h1), ("4h", h4)):
        if df is None or df.empty or not need.issubset(df.columns):
            raise ValueError(f"bad {name} ohlc")
    out = m15.copy().sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    closes = out["close"].astype(float).tolist()
    highs = out["high"].astype(float).tolist()
    lows = out["low"].astype(float).tolist()
    out["atr15"] = _atr_abs(highs, lows, closes)
    out["ema20_15"] = _ema(closes, 20)
    out["ema50_15"] = _ema(closes, 50)

    er = [float("nan")] * len(closes)
    for i in range(16, len(closes)):
        window = closes[i - 16 : i + 1]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[j] - window[j - 1]) for j in range(1, len(window)))
        er[i] = net / path if path > 1e-18 else float("nan")
    out["er_4h"] = er
    buf: list[float] = []
    exp = []
    for v in er:
        if not math.isnan(v):
            buf.append(v)
        exp.append(float(np.median(buf)) if buf else float("nan"))
    out["er_4h_exp_med"] = exp
    s_er = pd.Series(er, dtype=float)
    out["er_4h_roll_4320"] = s_er.rolling(4320, min_periods=max(200, 4320 // 10)).median()
    out["er_4h_roll_2880"] = s_er.rolling(2880, min_periods=max(200, 2880 // 10)).median()

    out = _join_htf(out, h1.sort_values("ts").drop_duplicates("ts"), "1h", 3_600_000)
    out = _join_htf(out, h4.sort_values("ts").drop_duplicates("ts"), "4h", 14_400_000)
    return out.reset_index(drop=True)


def _er_threshold(row: pd.Series, params: StrategyParams) -> float:
    col = {
        "expanding": "er_4h_exp_med",
        "rolling_4320": "er_4h_roll_4320",
        "rolling_2880": "er_4h_roll_2880",
    }.get(params.er4h_median_mode, "er_4h_roll_4320")
    v = row.get(col, float("nan"))
    return float(v) if not pd.isna(v) else float("nan")


def signal_at_bar(
    symbol: str,
    df: pd.DataFrame,
    i: int,
    btc_df: pd.DataFrame | None,
    params: StrategyParams | None = None,
) -> Signal | None:
    """Сигнал на закрытии 15m-бара i (бар полностью закрыт)."""
    params = params or StrategyParams()
    if i < 60 or i >= len(df):
        return None
    row = df.iloc[i]
    atr = float(row["atr15"])
    e20 = float(row["ema20_15"])
    e50 = float(row["ema50_15"])
    e20_1h = float(row["ema20_1h"]) if not pd.isna(row.get("ema20_1h", np.nan)) else float("nan")
    e50_1h = float(row["ema50_1h"]) if not pd.isna(row.get("ema50_1h", np.nan)) else float("nan")
    if math.isnan(atr) or atr <= 0 or math.isnan(e20_1h) or math.isnan(e50_1h):
        return None

    trend_long = e20_1h > e50_1h
    trend_short = e20_1h < e50_1h
    if not trend_long and not trend_short:
        return None

    if params.h1a_4h:
        e20_4h = float(row["ema20_4h"]) if not pd.isna(row.get("ema20_4h", np.nan)) else float("nan")
        e50_4h = float(row["ema50_4h"]) if not pd.isna(row.get("ema50_4h", np.nan)) else float("nan")
        if math.isnan(e20_4h) or math.isnan(e50_4h):
            return None
        if trend_long and not (e20_4h > e50_4h):
            return None
        if trend_short and not (e20_4h < e50_4h):
            return None

    er4 = float(row["er_4h"]) if not pd.isna(row["er_4h"]) else float("nan")
    if params.h1b_er4h:
        if math.isnan(er4):
            return None
        thr = _er_threshold(row, params)
        if math.isnan(thr) or er4 < thr:
            return None

    if params.btc_filter and btc_df is not None and len(btc_df):
        ts = int(row["ts"])
        b = btc_df[btc_df["ts"] <= ts].tail(1)
        if not b.empty:
            br = b.iloc[0]
            h1c = float(br["1h_close"]) if not pd.isna(br.get("1h_close", np.nan)) else float("nan")
            h1o = float(br["1h_open"]) if not pd.isna(br.get("1h_open", np.nan)) else float("nan")
            atr_b = float(br["atr_1h"]) if not pd.isna(br.get("atr_1h", np.nan)) else float("nan")
            if not math.isnan(h1c) and not math.isnan(h1o) and not math.isnan(atr_b) and atr_b > 0:
                move = h1c - h1o
                if trend_long and move < -1.5 * atr_b:
                    return None
                if trend_short and move > 1.5 * atr_b:
                    return None

    o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    band = 0.15 * atr

    if trend_long:
        side: Side = "long"
        if params.h2_impulse:
            z_lo, z_hi = min(e20, e50), max(e20, e50)
            if not (low <= z_hi + band and h >= z_lo - band):
                return None
            w0 = max(0, i - 12)
            ext_hi = float(df.iloc[w0 : i + 1]["high"].max())
            if ext_hi - min(c, e20) < 1.0 * atr:
                return None
            pull_low = float(df.iloc[w0 : i + 1]["low"].min())
            sl_ext = pull_low - 0.5 * atr
        else:
            if not ((low <= e20 + band) and (h >= e20 - band)):
                return None
            sl_ext = float(df.iloc[max(0, i - 5) : i + 1]["low"].min()) - 0.5 * atr
        if not (c > o and c > e20):
            return None
        limit_px = (c - 0.15 * atr) if params.use_h3_entry else (o + c) / 2.0
        min_sl = params.h2_min_sl_atr * atr if params.h2_min_sl_atr > 0 else 0.0
        risk = max(limit_px - sl_ext, min_sl)
        sl = limit_px - risk
    else:
        side = "short"
        if params.h2_impulse:
            z_lo, z_hi = min(e20, e50), max(e20, e50)
            if not (low <= z_hi + band and h >= z_lo - band):
                return None
            w0 = max(0, i - 12)
            ext_lo = float(df.iloc[w0 : i + 1]["low"].min())
            if max(c, e20) - ext_lo < 1.0 * atr:
                return None
            pull_hi = float(df.iloc[w0 : i + 1]["high"].max())
            sl_ext = pull_hi + 0.5 * atr
        else:
            if not ((low <= e20 + band) and (h >= e20 - band)):
                return None
            sl_ext = float(df.iloc[max(0, i - 5) : i + 1]["high"].max()) + 0.5 * atr
        if not (c < o and c < e20):
            return None
        limit_px = (c + 0.15 * atr) if params.use_h3_entry else (o + c) / 2.0
        min_sl = params.h2_min_sl_atr * atr if params.h2_min_sl_atr > 0 else 0.0
        risk = max(sl_ext - limit_px, min_sl)
        sl = limit_px + risk

    if risk <= 0 or risk / max(limit_px, 1e-12) < 1e-6:
        return None
    if side == "long":
        tp = limit_px + params.tp_r * risk
    else:
        tp = limit_px - params.tp_r * risk

    return Signal(
        symbol=symbol.upper(),
        side=side,
        bar_ts=int(row["ts"]),
        limit_px=float(limit_px),
        sl=float(sl),
        risk_px=float(risk),
        tp=float(tp),
        atr15=float(atr),
        er_4h=float(er4) if not math.isnan(er4) else float("nan"),
        confirm_close=float(c),
    )


def limit_filled(
    side: Side,
    limit_px: float,
    *,
    low: float,
    high: float,
    through_pct: float = 0.0,
) -> bool:
    """S0: касание; S1: пробитие на through_pct."""
    if through_pct > 0:
        if side == "long":
            return low < limit_px * (1.0 - through_pct)
        return high > limit_px * (1.0 + through_pct)
    if side == "long":
        return low <= limit_px
    return high >= limit_px


def size_leg_usd(
    *,
    entry: float,
    sl: float,
    fixed_leg: float,
    risk_clamp: bool,
    target_risk_usd: float,
    leg_min: float,
    leg_max: float,
    min_usd: float = 0.0,
) -> float:
    sl_pct = abs(entry - sl) / entry if entry > 0 else 0.0
    if risk_clamp and sl_pct > 1e-8:
        leg = max(leg_min, min(leg_max, target_risk_usd / sl_pct))
    else:
        leg = fixed_leg
    return max(leg, float(min_usd or 0.0))
