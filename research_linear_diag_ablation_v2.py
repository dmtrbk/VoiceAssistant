#!/usr/bin/env python3
"""
Диагностика v1 (A maker, BTC on) + ablation v2 без перебора мелких порогов.

Запуск:
  .venv/bin/python research_linear_diag_ablation_v2.py
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research_linear.metrics import efficiency_ratio, ema

HIST = Path("research_linear_data/history")
OUT = Path("research_linear_data/diag_ablation_v2")
SCREENER_MD = Path("research_linear_data/screener_crypto_top15.md")
SYMBOLS = ["SOLUSDT", "HYPEUSDT", "ZECUSDT"]
BTC = "BTCUSDT"

MAKER = 0.0002
TAKER = 0.00055
SLIP = 0.0002
LEG_USD = 40.0
MAX_POS = 2
START_CAP = 160.0


@dataclass
class Cfg:
    name: str
    btc_filter: bool = True
    entry_mode: str = "maker"  # maker|taker
    # H1
    h1a_4h: bool = False
    h1b_er4h: bool = False
    er4h_fixed: float | None = None  # if set, use fixed; else expanding median
    # H2
    h2_impulse: bool = False
    h2_min_sl_atr: float = 0.0  # 1.5 when H2 on
    # H3
    h3_entry_near: bool = False  # limit = close ± 0.15 ATR
    ttl: int = 2
    # exit
    exit_mode: str = "tp2r"  # tp2r|tp2_5r|trail
    tp_r: float = 2.0
    trail_arm_r: float = 1.0
    trail_atr_mult: float = 2.0
    # stress / costs
    maker_fee: float = MAKER
    taker_fee: float = TAKER
    slip_exit: float = SLIP  # SL / market exit slippage
    slip_entry: float = SLIP  # taker entry slippage
    fill_through_pct: float = 0.0  # S1: require trade through limit by this fraction (0.0002=0.02%)
    # sizing
    leg_usd: float = LEG_USD  # fixed notional if risk_clamp off
    risk_clamp: bool = False
    target_risk_usd: float = 0.65
    leg_min_usd: float = 25.0
    leg_max_usd: float = 50.0
    min_usd_floor: float = 0.0  # per-symbol min order floor applied at fill
    # H1b median mode: expanding | rolling_4320 | rolling_2880
    er4h_median_mode: str = "expanding"


@dataclass
class Pending:
    symbol: str
    side: str
    limit_px: float
    sl: float
    risk_px: float
    created_i: int
    expire_i: int
    atr15: float
    er_4h: float
    confirm_close: float


@dataclass
class Pos:
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float
    risk_px: float
    open_i: int
    open_ts: int
    atr15: float
    er_4h: float
    fees: float = 0.0
    funding: float = 0.0
    slip: float = 0.0
    realized: float = 0.0
    trail_armed: bool = False
    entry_mode: str = "maker"
    leg_usd: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float
    fees: float
    funding: float
    slip: float
    exit_reason: str
    er_4h: float
    sl_pct: float
    hold_bars: int
    entry_mode: str
    leg_usd: float = 0.0


@dataclass
class Stats:
    signals: int = 0  # setups that passed filters (attempted place or blocked)
    orders: int = 0  # limits actually placed
    fills: int = 0
    signals_blocked_cap: int = 0
    fill_skip_maxpos: int = 0
    fill_skip_same_sym: int = 0
    fill_miss_ttl: int = 0  # expired without fill
    fill_skip_taker_window: int = 0  # taker missed unique +1 bar due to cap


def load_ohlc(symbol: str, tf: str) -> pd.DataFrame:
    pq = HIST / f"{symbol}_{tf}.parquet"
    csv = HIST / f"{symbol}_{tf}.csv"
    df = pd.read_parquet(pq) if pq.exists() else pd.read_csv(csv)
    return df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def load_funding(symbol: str) -> pd.DataFrame:
    pq = HIST / f"{symbol}_funding.parquet"
    csv = HIST / f"{symbol}_funding.csv"
    if pq.exists():
        df = pd.read_parquet(pq)
    elif csv.exists():
        df = pd.read_csv(csv)
    else:
        return pd.DataFrame(columns=["fundingRateTimestamp", "fundingRate"])
    if df.empty:
        return df
    return df.sort_values("fundingRateTimestamp").drop_duplicates("fundingRateTimestamp").reset_index(drop=True)


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
    h["ema20"] = ema(closes, 20)
    h["ema50"] = ema(closes, 50)
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
    return pd.merge_asof(
        m15.sort_values("ts"),
        right.sort_values("close_ts"),
        left_on="ts",
        right_on="close_ts",
        direction="backward",
        suffixes=("", f"_{prefix}_dup"),
    )


def build_features(symbol: str) -> pd.DataFrame:
    m15 = load_ohlc(symbol, "15m").copy()
    h1 = load_ohlc(symbol, "1h")
    h4 = load_ohlc(symbol, "4h")
    closes = m15["close"].astype(float).tolist()
    highs = m15["high"].astype(float).tolist()
    lows = m15["low"].astype(float).tolist()
    m15["atr15"] = _atr_abs(highs, lows, closes)
    m15["ema20_15"] = ema(closes, 20)
    m15["ema50_15"] = ema(closes, 50)

    # rolling ER_4h on 16×15m bars, causal
    er = [float("nan")] * len(closes)
    for i in range(16, len(closes)):
        window = closes[i - 16 : i + 1]
        # efficiency on last 16 steps ending at i
        net = abs(window[-1] - window[0])
        path = sum(abs(window[j] - window[j - 1]) for j in range(1, len(window)))
        er[i] = net / path if path > 1e-18 else float("nan")
    m15["er_4h"] = er
    # expanding median of er_4h (causal)
    er_med = []
    buf: list[float] = []
    for v in er:
        if not math.isnan(v):
            buf.append(v)
        er_med.append(float(np.median(buf)) if buf else float("nan"))
    m15["er_4h_exp_med"] = er_med
    # causal rolling medians for live warm-up parity
    s_er = pd.Series(er, dtype=float)
    m15["er_4h_roll_4320"] = s_er.rolling(4320, min_periods=max(200, 4320 // 10)).median()
    m15["er_4h_roll_2880"] = s_er.rolling(2880, min_periods=max(200, 2880 // 10)).median()

    m15 = _join_htf(m15, h1, "1h", 3_600_000)
    # drop helper close_ts if duplicated
    if "close_ts" in m15.columns:
        m15 = m15.drop(columns=["close_ts"], errors="ignore")
    m15 = _join_htf(m15, h4, "4h", 14_400_000)
    if "close_ts" in m15.columns:
        m15 = m15.drop(columns=["close_ts"], errors="ignore")
    return m15


def limit_hit(side: str, px: float, bar: pd.Series, through_pct: float = 0.0) -> bool:
    """Maker fill: касание low<=limit / high>=limit; при through_pct>0 — строго глубже."""
    lo, hi = float(bar["low"]), float(bar["high"])
    if through_pct > 0:
        if side == "long":
            return lo < px * (1.0 - through_pct)
        return hi > px * (1.0 + through_pct)
    if side == "long":
        return lo <= px
    return hi >= px


def mkt_px(side: str, px: float, *, exit_: bool, slip: float = SLIP) -> tuple[float, float]:
    if side == "long" and not exit_:
        ep = px * (1 + slip)
    elif side == "long" and exit_:
        ep = px * (1 - slip)
    elif side == "short" and not exit_:
        ep = px * (1 - slip)
    else:
        ep = px * (1 + slip)
    return ep, abs(ep - px) / px


def try_signal(df: pd.DataFrame, i: int, btc_df: pd.DataFrame | None, cfg: Cfg) -> Pending | None:
    if i < 60:
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

    # H1a: 4h trend
    if cfg.h1a_4h:
        e20_4h = float(row["ema20_4h"]) if not pd.isna(row.get("ema20_4h", np.nan)) else float("nan")
        e50_4h = float(row["ema50_4h"]) if not pd.isna(row.get("ema50_4h", np.nan)) else float("nan")
        if math.isnan(e20_4h) or math.isnan(e50_4h):
            return None
        if trend_long and not (e20_4h > e50_4h):
            return None
        if trend_short and not (e20_4h < e50_4h):
            return None

    er4 = float(row["er_4h"]) if not pd.isna(row["er_4h"]) else float("nan")
    if cfg.h1b_er4h:
        if math.isnan(er4):
            return None
        if cfg.er4h_fixed is not None:
            thr = cfg.er4h_fixed
        else:
            col = {
                "expanding": "er_4h_exp_med",
                "rolling_4320": "er_4h_roll_4320",
                "rolling_2880": "er_4h_roll_2880",
            }.get(cfg.er4h_median_mode, "er_4h_exp_med")
            thr = float(row[col]) if not pd.isna(row.get(col, np.nan)) else float("nan")
            if math.isnan(thr):
                return None
        if er4 < thr:
            return None

    # BTC filter
    if cfg.btc_filter and btc_df is not None:
        ts = int(row["ts"])
        b = btc_df[btc_df["ts"] <= ts].tail(1)
        if not b.empty:
            br = b.iloc[0]
            h1c = float(br.get("1h_close", br.get("h1_close", float("nan"))))
            h1o = float(br.get("1h_open", br.get("h1_open", float("nan"))))
            atr_b = float(br.get("atr_1h", br.get("atr1h", float("nan"))))
            # column names from our join
            if "1h_close" in br.index:
                h1c = float(br["1h_close"]) if not pd.isna(br["1h_close"]) else float("nan")
            if "1h_open" in br.index:
                h1o = float(br["1h_open"]) if not pd.isna(br["1h_open"]) else float("nan")
            if "atr_1h" in br.index:
                atr_b = float(br["atr_1h"]) if not pd.isna(br["atr_1h"]) else float("nan")
            if not math.isnan(h1c) and not math.isnan(h1o) and not math.isnan(atr_b) and atr_b > 0:
                move = h1c - h1o
                if trend_long and move < -1.5 * atr_b:
                    return None
                if trend_short and move > 1.5 * atr_b:
                    return None

    o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    band = 0.15 * atr

    if trend_long:
        side = "long"
        # EMA zone touch
        if cfg.h2_impulse:
            # zone EMA20–EMA50
            z_lo, z_hi = min(e20, e50), max(e20, e50)
            touched = low <= z_hi + band and h >= z_lo - band
            if not touched:
                return None
            # impulse: from 12-bar high, pullback >= 1.0 ATR
            w0 = max(0, i - 12)
            ext = float(df.iloc[w0 : i + 1]["high"].max())
            depth = ext - min(c, e20)  # pullback depth toward EMA
            if depth < 1.0 * atr:
                return None
        else:
            touched = (low <= e20 + band) and (h >= e20 - band)
            if not touched:
                return None
            ext = float(df.iloc[max(0, i - 5) : i + 1]["low"].min())

        if not (c > o and c > e20):
            return None

        if cfg.h3_entry_near:
            limit_px = c - 0.15 * atr
        else:
            limit_px = (o + c) / 2.0

        sl_ext = ext - 0.5 * atr if cfg.h2_impulse else float(df.iloc[max(0, i - 5) : i + 1]["low"].min()) - 0.5 * atr
        # for non-H2 long, ext was low
        if not cfg.h2_impulse:
            sl_ext = ext - 0.5 * atr
        else:
            # H2 long: SL beyond pullback low (use 12-bar low) + 0.5 ATR
            pull_low = float(df.iloc[max(0, i - 12) : i + 1]["low"].min())
            sl_ext = pull_low - 0.5 * atr

        min_sl = (cfg.h2_min_sl_atr * atr) if cfg.h2_min_sl_atr > 0 else 0.0
        risk_needed = max(limit_px - sl_ext, min_sl)
        sl = limit_px - risk_needed
        risk = limit_px - sl

    else:
        side = "short"
        if cfg.h2_impulse:
            z_lo, z_hi = min(e20, e50), max(e20, e50)
            touched = low <= z_hi + band and h >= z_lo - band
            if not touched:
                return None
            w0 = max(0, i - 12)
            ext = float(df.iloc[w0 : i + 1]["low"].min())
            depth = max(c, e20) - ext
            if depth < 1.0 * atr:
                return None
        else:
            touched = (low <= e20 + band) and (h >= e20 - band)
            if not touched:
                return None
            ext = float(df.iloc[max(0, i - 5) : i + 1]["high"].max())

        if not (c < o and c < e20):
            return None

        if cfg.h3_entry_near:
            limit_px = c + 0.15 * atr
        else:
            limit_px = (o + c) / 2.0

        if cfg.h2_impulse:
            pull_hi = float(df.iloc[max(0, i - 12) : i + 1]["high"].max())
            sl_ext = pull_hi + 0.5 * atr
        else:
            sl_ext = ext + 0.5 * atr

        min_sl = (cfg.h2_min_sl_atr * atr) if cfg.h2_min_sl_atr > 0 else 0.0
        risk_needed = max(sl_ext - limit_px, min_sl)
        sl = limit_px + risk_needed
        risk = sl - limit_px

    if risk <= 0 or risk / max(limit_px, 1e-12) < 1e-6:
        return None

    return Pending(
        symbol="",
        side=side,
        limit_px=limit_px,
        sl=sl,
        risk_px=risk,
        created_i=i,
        expire_i=i + cfg.ttl,
        atr15=atr,
        er_4h=er4 if not math.isnan(er4) else float("nan"),
        confirm_close=c,
    )


def close_pos(
    pos: Pos,
    exit_raw: float,
    ts: int,
    i: int,
    reason: str,
    trades: list[Trade],
    *,
    slip_exit: float = SLIP,
    taker_fee: float = TAKER,
) -> None:
    ep, _slip_frac = mkt_px(pos.side, exit_raw, exit_=True, slip=slip_exit)
    qty = pos.qty
    fee = abs(ep * qty) * taker_fee
    slip = abs(ep - exit_raw) * qty
    if pos.side == "long":
        pnl = (ep - pos.entry) * qty
    else:
        pnl = (pos.entry - ep) * qty
    fund = pos.funding
    pnl_net = pnl - fee - slip + fund
    r = pnl_net / (pos.risk_px * qty) if pos.risk_px * qty > 0 else 0.0
    trades.append(
        Trade(
            symbol=pos.symbol,
            side=pos.side,
            entry_ts=pos.open_ts,
            exit_ts=ts,
            entry=pos.entry,
            exit=ep,
            qty=qty,
            pnl=pnl_net,
            r_multiple=r,
            fees=pos.fees + fee,
            funding=fund,
            slip=pos.slip + slip,
            exit_reason=reason,
            er_4h=pos.er_4h,
            sl_pct=abs(pos.entry - pos.sl) / pos.entry * 100.0,
            hold_bars=max(0, i - pos.open_i),
            entry_mode=pos.entry_mode,
            leg_usd=pos.leg_usd,
        )
    )


def run(cfg: Cfg, feats: dict[str, pd.DataFrame], funds: dict[str, pd.DataFrame], btc_df: pd.DataFrame) -> dict[str, Any]:
    positions: list[Pos] = []
    pending: list[Pending] = []
    trades: list[Trade] = []
    stats = {s: Stats() for s in feats}
    global_stats = Stats()
    equity: list[float] = []
    realized_eq = 0.0
    max_pos_full_bars = 0  # сколько баров (ts) было ровно 2/2 открытых

    all_ts = sorted(set().union(*[set(df["ts"].astype("int64")) for df in feats.values()]))
    idx = {sym: {int(t): i for i, t in enumerate(df["ts"].astype("int64"))} for sym, df in feats.items()}
    fund_i = {sym: 0 for sym in feats}

    for ts in all_ts:
        # funding
        for sym, df in feats.items():
            fdf = funds[sym]
            if fdf.empty:
                continue
            fi = fund_i[sym]
            fts = fdf["fundingRateTimestamp"].astype("int64")
            while fi < len(fts) and int(fts.iloc[fi]) <= ts:
                rate = float(fdf["fundingRate"].iloc[fi])
                for p in positions:
                    if p.symbol == sym:
                        notion = p.entry * p.qty
                        cash = -notion * rate if p.side == "long" else notion * rate
                        p.funding += cash
                        p.realized += cash
                fi += 1
            fund_i[sym] = fi

        # per-symbol update
        for sym, df in feats.items():
            i = idx[sym].get(ts)
            if i is None:
                continue
            row = df.iloc[i]
            o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
            atr = float(row["atr15"]) if not pd.isna(row["atr15"]) else float("nan")

            # manage positions
            still: list[Pos] = []
            for pos in positions:
                if pos.symbol != sym:
                    still.append(pos)
                    continue
                hit_sl = (pos.side == "long" and low <= pos.sl) or (pos.side == "short" and h >= pos.sl)
                r = pos.risk_px
                if pos.side == "long":
                    tp = pos.entry + cfg.tp_r * r
                    arm = pos.entry + cfg.trail_arm_r * r
                else:
                    tp = pos.entry - cfg.tp_r * r
                    arm = pos.entry - cfg.trail_arm_r * r

                if cfg.exit_mode in {"tp2r", "tp2_5r"}:
                    hit_tp = (pos.side == "long" and h >= tp) or (pos.side == "short" and low <= tp)
                    if hit_sl:
                        close_pos(
                            pos, pos.sl, ts, i, "sl", trades,
                            slip_exit=cfg.slip_exit, taker_fee=cfg.taker_fee,
                        )
                        realized_eq += trades[-1].pnl
                    elif hit_tp:
                        close_pos(
                            pos, tp, ts, i, f"tp{cfg.tp_r}R", trades,
                            slip_exit=cfg.slip_exit, taker_fee=cfg.taker_fee,
                        )
                        realized_eq += trades[-1].pnl
                    else:
                        still.append(pos)
                else:  # trail
                    # arm after +1R
                    hit_arm = (pos.side == "long" and h >= arm) or (pos.side == "short" and low <= arm)
                    if hit_arm:
                        pos.trail_armed = True
                    if pos.trail_armed and not math.isnan(atr):
                        if pos.side == "long":
                            pos.sl = max(pos.sl, c - cfg.trail_atr_mult * atr)
                        else:
                            pos.sl = min(pos.sl, c + cfg.trail_atr_mult * atr)
                    hit_sl2 = (pos.side == "long" and low <= pos.sl) or (pos.side == "short" and h >= pos.sl)
                    if hit_sl2:
                        reason = "trail" if pos.trail_armed else "sl"
                        close_pos(
                            pos, pos.sl, ts, i, reason, trades,
                            slip_exit=cfg.slip_exit, taker_fee=cfg.taker_fee,
                        )
                        realized_eq += trades[-1].pnl
                    else:
                        still.append(pos)
            positions = still

            # pending fills
            kept: list[Pending] = []
            for pend in pending:
                if pend.symbol != sym:
                    kept.append(pend)
                    continue
                if i > pend.expire_i:
                    stats[sym].fill_miss_ttl += 1
                    global_stats.fill_miss_ttl += 1
                    continue
                if i <= pend.created_i:
                    kept.append(pend)
                    continue

                if len(positions) >= MAX_POS:
                    stats[sym].fill_skip_maxpos += 1
                    global_stats.fill_skip_maxpos += 1
                    if cfg.entry_mode == "taker" and i == pend.created_i + 1:
                        stats[sym].fill_skip_taker_window += 1
                        global_stats.fill_skip_taker_window += 1
                    if i < pend.expire_i:
                        kept.append(pend)
                    else:
                        stats[sym].fill_miss_ttl += 1
                    continue
                if any(p.symbol == sym for p in positions):
                    stats[sym].fill_skip_same_sym += 1
                    global_stats.fill_skip_same_sym += 1
                    if i < pend.expire_i:
                        kept.append(pend)
                    continue

                filled = False
                entry_px = pend.limit_px
                fee_mode = "maker"
                fee_rate = cfg.maker_fee
                slip_cost = 0.0

                if cfg.entry_mode == "maker":
                    if limit_hit(pend.side, pend.limit_px, row, through_pct=cfg.fill_through_pct):
                        filled = True
                else:
                    if i == pend.created_i + 1:
                        filled = True
                        entry_px, _sf = mkt_px(pend.side, o, exit_=False, slip=cfg.slip_entry)
                        fee_mode = "taker"
                        fee_rate = cfg.taker_fee
                        slip_cost = abs(entry_px - o) * (LEG_USD / entry_px)
                    else:
                        # past unique taker window
                        if i > pend.created_i + 1:
                            stats[sym].fill_skip_taker_window += 1
                            global_stats.fill_skip_taker_window += 1
                            continue  # dead for taker

                if filled:
                    stats[sym].fills += 1
                    global_stats.fills += 1
                    # sizing: fixed leg or risk-clamped
                    sl_pct = abs(entry_px - pend.sl) / entry_px if entry_px > 0 else 0.0
                    if cfg.risk_clamp and sl_pct > 1e-8:
                        leg = cfg.target_risk_usd / sl_pct
                        leg = max(cfg.leg_min_usd, min(cfg.leg_max_usd, leg))
                    else:
                        leg = cfg.leg_usd
                    leg = max(leg, float(cfg.min_usd_floor or 0.0))
                    qty = leg / entry_px
                    if cfg.entry_mode != "maker":
                        slip_cost = abs(entry_px - o) * qty
                    fee = abs(entry_px * qty) * fee_rate
                    positions.append(
                        Pos(
                            symbol=sym,
                            side=pend.side,
                            qty=qty,
                            entry=entry_px,
                            sl=pend.sl,
                            risk_px=abs(pend.risk_px),
                            open_i=i,
                            open_ts=ts,
                            atr15=pend.atr15,
                            er_4h=pend.er_4h,
                            fees=fee,
                            slip=slip_cost,
                            realized=-fee - slip_cost,
                            entry_mode=fee_mode,
                            leg_usd=leg,
                        )
                    )
                elif i < pend.expire_i:
                    kept.append(pend)
                else:
                    stats[sym].fill_miss_ttl += 1
                    global_stats.fill_miss_ttl += 1
            pending = kept

            # new signals
            sig = try_signal(df, i, btc_df, cfg)
            if sig is None:
                continue
            sig.symbol = sym
            stats[sym].signals += 1
            global_stats.signals += 1

            if len(positions) >= MAX_POS or any(p.symbol == sym for p in positions) or any(
                p.symbol == sym for p in pending
            ):
                stats[sym].signals_blocked_cap += 1
                global_stats.signals_blocked_cap += 1
                continue

            pending.append(sig)
            stats[sym].orders += 1
            global_stats.orders += 1

        if len(positions) >= MAX_POS:
            max_pos_full_bars += 1

        # equity mark
        mark = realized_eq
        for p in positions:
            ii = idx[p.symbol].get(ts)
            if ii is None:
                continue
            cc = float(feats[p.symbol].iloc[ii]["close"])
            if p.side == "long":
                mark += (cc - p.entry) * p.qty + p.realized
            else:
                mark += (p.entry - cc) * p.qty + p.realized
        equity.append(mark)

    # EOD
    for pos in list(positions):
        df = feats[pos.symbol]
        last_i = len(df) - 1
        last = df.iloc[last_i]
        close_pos(
            pos, float(last["close"]), int(last["ts"]), last_i, "eod", trades,
            slip_exit=cfg.slip_exit, taker_fee=cfg.taker_fee,
        )
        realized_eq += trades[-1].pnl
    positions.clear()

    return {
        "cfg": cfg.name,
        "trades": trades,
        "stats": stats,
        "global_stats": global_stats,
        "equity": equity,
        "max_pos_full_bars": max_pos_full_bars,
    }


def summarize_trades(trades: list[Trade], orders: int, fills: int) -> dict[str, float]:
    if not trades:
        return {
            "fills": 0,
            "fill_rate": 0.0,
            "WR": 0.0,
            "PF": 0.0,
            "Exp_R": 0.0,
            "Exp_usd": 0.0,
            "PnL": 0.0,
            "MaxDD": 0.0,
            "fees_slip": 0.0,
            "avg_win_R": 0.0,
            "avg_loss_R": 0.0,
            "avg_sl_pct": 0.0,
            "avg_hold_bars": 0.0,
            "n": 0,
        }
    pnls = [t.pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    eq = peak = max_dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    return {
        "fills": fills,
        "fill_rate": fills / orders if orders else 0.0,
        "WR": len(wins) / len(pnls),
        "PF": (gw / gl) if gl > 0 else float("inf"),
        "Exp_R": float(np.mean(rs)),
        "Exp_usd": float(np.mean(pnls)),
        "PnL": float(sum(pnls)),
        "MaxDD": abs(max_dd),
        "fees_slip": float(sum(t.fees + t.slip for t in trades)),
        "avg_win_R": float(np.mean(win_r)) if win_r else 0.0,
        "avg_loss_R": float(np.mean(loss_r)) if loss_r else 0.0,
        "avg_sl_pct": float(np.mean([t.sl_pct for t in trades])),
        "avg_hold_bars": float(np.mean([t.hold_bars for t in trades])),
        "n": len(pnls),
    }


def fmt_row(name: str, s: dict) -> str:
    return (
        f"| {name} | {int(s['fills'])} | {s['fill_rate']:.1%} | {s['WR']:.1%} | {s['PF']:.2f} | "
        f"{s['Exp_R']:.3f} | {s['Exp_usd']:.3f} | {s['PnL']:.2f} | {s['MaxDD']:.2f} | {s['fees_slip']:.2f} |"
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # --- Part 1: screener ---
    print("=" * 80)
    print("SCREENER screener_crypto_top15.md (full)")
    print("=" * 80)
    if SCREENER_MD.exists():
        text = SCREENER_MD.read_text(encoding="utf-8")
        print(text)
        (OUT / "screener_crypto_top15.md").write_text(text, encoding="utf-8")
    else:
        print("MISSING", SCREENER_MD)
        return 1

    print("Building features…", flush=True)
    feats: dict[str, pd.DataFrame] = {}
    funds: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS + [BTC]:
        feats[sym] = build_features(sym)
        funds[sym] = load_funding(sym)
        print(f"  {sym}: {len(feats[sym])} bars", flush=True)
    btc_df = feats[BTC]
    trade_feats = {s: feats[s] for s in SYMBOLS}

    # --- Baseline A maker BTC on ---
    baseline = Cfg(name="Baseline_v1", btc_filter=True, entry_mode="maker", exit_mode="tp2r", tp_r=2.0)
    print("\nRunning baseline A maker BTC-on…", flush=True)
    base_res = run(baseline, trade_feats, funds, btc_df)

    # Taker for fill explanation
    print("Running taker diagnostic…", flush=True)
    taker_cfg = Cfg(name="taker_diag", btc_filter=True, entry_mode="taker", exit_mode="tp2r", tp_r=2.0)
    taker_res = run(taker_cfg, trade_feats, funds, btc_df)

    def diag_block(res: dict, title: str) -> str:
        lines = [f"# {title}", ""]
        trades: list[Trade] = res["trades"]
        stats: dict[str, Stats] = res["stats"]
        g: Stats = res["global_stats"]

        lines.append("## By ticker")
        lines.append(
            "| symbol | signals | orders | fills | fill_rate | WR | avg_win_R | avg_loss_R | PF | Exp_R | Exp_usd | PnL | avg_sl_pct | avg_hold_bars |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for sym in SYMBOLS:
            st = stats[sym]
            tr = [t for t in trades if t.symbol == sym]
            s = summarize_trades(tr, st.orders, st.fills)
            fr = st.fills / st.orders if st.orders else 0.0
            lines.append(
                f"| {sym} | {st.signals} | {st.orders} | {st.fills} | {fr:.1%} | {s['WR']:.1%} | "
                f"{s['avg_win_R']:.2f} | {s['avg_loss_R']:.2f} | {s['PF']:.2f} | {s['Exp_R']:.3f} | "
                f"{s['Exp_usd']:.3f} | {s['PnL']:.2f} | {s['avg_sl_pct']:.3f} | {s['avg_hold_bars']:.1f} |"
            )
        # portfolio
        s_all = summarize_trades(trades, g.orders, g.fills)
        lines.append(
            f"| ALL | {g.signals} | {g.orders} | {g.fills} | {s_all['fill_rate']:.1%} | {s_all['WR']:.1%} | "
            f"{s_all['avg_win_R']:.2f} | {s_all['avg_loss_R']:.2f} | {s_all['PF']:.2f} | {s_all['Exp_R']:.3f} | "
            f"{s_all['Exp_usd']:.3f} | {s_all['PnL']:.2f} | {s_all['avg_sl_pct']:.3f} | {s_all['avg_hold_bars']:.1f} |"
        )

        lines.append("")
        lines.append("## By side (Long vs Short)")
        lines.append("| side | fills | WR | PF | Exp_R | PnL |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for side in ("long", "short"):
            tr = [t for t in trades if t.side == side]
            s = summarize_trades(tr, len(tr), len(tr))
            lines.append(
                f"| {side} | {len(tr)} | {s['WR']:.1%} | {s['PF']:.2f} | {s['Exp_R']:.3f} | {s['PnL']:.2f} |"
            )

        lines.append("")
        lines.append("## By er_4h tertile at entry")
        lines.append("| tertile | fills | WR | PF | Exp_R | PnL | er_4h range |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        ers = [t.er_4h for t in trades if not math.isnan(t.er_4h)]
        if ers:
            q1, q2 = np.quantile(ers, [1 / 3, 2 / 3])
            buckets = [
                ("low", -1e9, q1),
                ("mid", q1, q2),
                ("high", q2, 1e9),
            ]
            for name, a, b in buckets:
                tr = [t for t in trades if not math.isnan(t.er_4h) and a <= t.er_4h <= b]
                # fix edges: low < q1, mid q1..q2, high > q2
                if name == "low":
                    tr = [t for t in trades if not math.isnan(t.er_4h) and t.er_4h < q1]
                elif name == "mid":
                    tr = [t for t in trades if not math.isnan(t.er_4h) and q1 <= t.er_4h < q2]
                else:
                    tr = [t for t in trades if not math.isnan(t.er_4h) and t.er_4h >= q2]
                s = summarize_trades(tr, len(tr), len(tr))
                if tr:
                    er_lo = min(t.er_4h for t in tr)
                    er_hi = max(t.er_4h for t in tr)
                    rng = f"{er_lo:.3f}…{er_hi:.3f}"
                else:
                    rng = "—"
                lines.append(
                    f"| {name} | {len(tr)} | {s['WR']:.1%} | {s['PF']:.2f} | {s['Exp_R']:.3f} | {s['PnL']:.2f} | {rng} |"
                )
            lines.append(f"\nTertile cuts: q1={q1:.4f}, q2={q2:.4f}")
        else:
            lines.append("| — | 0 | — | — | — | — | — |")

        return "\n".join(lines)

    diag_md = diag_block(base_res, "Диагностика v1 — A maker, BTC filter ON")
    # taker explanation
    tg: Stats = taker_res["global_stats"]
    diag_md += "\n\n## Почему taker fill ≈ 90%, а не 100%\n\n"
    diag_md += (
        f"- Taker diagnostic: orders={tg.orders}, fills={tg.fills}, "
        f"fill_rate={tg.fills/tg.orders if tg.orders else 0:.1%}\n"
        f"- `fill_skip_maxpos` (блок на баре входа из‑за max_pos=2): {tg.fill_skip_maxpos}\n"
        f"- `fill_skip_taker_window` (уникальное окно `created_i+1` пропущено из‑за cap / опоздание): "
        f"{tg.fill_skip_taker_window}\n"
        f"- `fill_skip_same_sym`: {tg.fill_skip_same_sym}\n"
        f"- `fill_miss_ttl` (истечение TTL без fill): {tg.fill_miss_ttl}\n"
        f"- `signals_blocked_cap` (сигнал был, ордер не выставили): {tg.signals_blocked_cap}\n\n"
        "Логика: taker исполняется **только на баре `created_i+1`**. Если в этот момент "
        "`len(positions) >= 2` (другой символ успел открыться на том же ts / предыдущем баре), "
        "ордер остаётся pending, но на барах `> created_i+1` taker уже **не заполняется** и "
        "сгорает по TTL. Отсюда ~10% «дыр», а не промах лимитки по цене. "
        "Maker может добрать fill на `created_i+1` или `+2`, поэтому его fill_rate ниже по цене, "
        "но taker не 100% из‑за конкуренции за слот max_pos=2.\n"
    )

    print("\n" + diag_md)
    (OUT / "diag_v1_A_maker_btc_on.md").write_text(diag_md + "\n", encoding="utf-8")

    # --- Part 2: ablations ---
    ablations = [
        Cfg(name="Baseline_v1", btc_filter=True, entry_mode="maker", exit_mode="tp2r", tp_r=2.0),
        Cfg(name="+H1a", btc_filter=True, entry_mode="maker", h1a_4h=True, exit_mode="tp2r", tp_r=2.0),
        Cfg(name="+H1b", btc_filter=True, entry_mode="maker", h1b_er4h=True, exit_mode="tp2r", tp_r=2.0),
        Cfg(
            name="+H2",
            btc_filter=True,
            entry_mode="maker",
            h2_impulse=True,
            h2_min_sl_atr=1.5,
            exit_mode="tp2r",
            tp_r=2.0,
        ),
        Cfg(
            name="+H1+H2",
            btc_filter=True,
            entry_mode="maker",
            h1a_4h=True,
            h1b_er4h=True,
            h2_impulse=True,
            h2_min_sl_atr=1.5,
            exit_mode="tp2r",
            tp_r=2.0,
        ),
        Cfg(
            name="+H1+H2+H3_entry",
            btc_filter=True,
            entry_mode="maker",
            h1a_4h=True,
            h1b_er4h=True,
            h2_impulse=True,
            h2_min_sl_atr=1.5,
            h3_entry_near=True,
            ttl=2,
            exit_mode="tp2r",
            tp_r=2.0,
        ),
        Cfg(
            name="+H1+H2+TP2.5R",
            btc_filter=True,
            entry_mode="maker",
            h1a_4h=True,
            h1b_er4h=True,
            h2_impulse=True,
            h2_min_sl_atr=1.5,
            exit_mode="tp2_5r",
            tp_r=2.5,
        ),
        Cfg(
            name="+H1+H2+Trail",
            btc_filter=True,
            entry_mode="maker",
            h1a_4h=True,
            h1b_er4h=True,
            h2_impulse=True,
            h2_min_sl_atr=1.5,
            exit_mode="trail",
            tp_r=2.0,
            trail_arm_r=1.0,
            trail_atr_mult=2.0,
        ),
    ]

    # H1 in combo uses both H1a+H1b as "H1" per expert " +H1+H2 "
    rows = []
    ablation_md = [
        "# Ablation v2 (A maker, BTC on)",
        "",
        "H1 в комбинациях = H1a (4h EMA) **и** H1b (er_4h ≥ expanding median по монете).",
        "H2 = импульс ≥1.0·ATR15 от 12-бар экстремума + min SL = max(ext+0.5ATR, 1.5ATR).",
        "H3_entry = лимит у confirm_close ± 0.15·ATR15, TTL=2.",
        "Trail = активация после +1.0R, шаг 2.0·ATR15, без partial и без BE.",
        "",
        "| variant | fills | fill_rate | WR | PF | Exp_R | Exp_usd | PnL | MaxDD | fees+slip |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for cfg in ablations:
        print(f"Ablation {cfg.name}…", flush=True)
        if cfg.name == "Baseline_v1":
            res = base_res
        else:
            res = run(cfg, trade_feats, funds, btc_df)
        g: Stats = res["global_stats"]
        s = summarize_trades(res["trades"], g.orders, g.fills)
        # use orders-based fill_rate
        s["fills"] = g.fills
        s["fill_rate"] = g.fills / g.orders if g.orders else 0.0
        ablation_md.append(fmt_row(cfg.name, s))
        rows.append({"variant": cfg.name, **s, "signals": g.signals, "orders": g.orders})
        # save trades
        pd.DataFrame([asdict(t) for t in res["trades"]]).to_csv(
            OUT / f"trades_{cfg.name.replace('+', 'plus').replace('.', '_')}.csv", index=False
        )

    ablation_text = "\n".join(ablation_md) + "\n"
    print("\n" + ablation_text)
    (OUT / "ablation_v2.md").write_text(ablation_text, encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT / "ablation_v2.csv", index=False)

    # combined report
    full = (
        "# Report: screener + diag v1 + ablation v2\n\n"
        + text
        + "\n\n---\n\n"
        + diag_md
        + "\n\n---\n\n"
        + ablation_text
    )
    (OUT / "REPORT.md").write_text(full, encoding="utf-8")
    print(f"\nArtifacts → {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
