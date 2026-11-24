#!/usr/bin/env python3
"""
Каркас бэктеста v1: откат к EMA на Bybit USDT Perp (15m / 1h).
Baseline без подбора параметров. Читает research_linear_data/history/.

Запуск:
  .venv/bin/python research_linear_backtest_v1.py
  .venv/bin/python research_linear_backtest_v1.py --btc-filter
  .venv/bin/python research_linear_backtest_v1.py --entry-mode taker
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from research_linear.metrics import atr_pct_series, ema

HIST = Path("research_linear_data/history")
OUT = Path("research_linear_data/backtest_v1")
TRADE_SYMBOLS = ["SOLUSDT", "HYPEUSDT", "ZECUSDT"]
BTC = "BTCUSDT"

MAKER = 0.0002
TAKER = 0.00055
SLIP = 0.0002  # 0.02% поверх на market/stop
LEG_USD = 40.0
MAX_POS = 2
LIMIT_TTL = 2  # 15m bars


@dataclass
class PendingLimit:
    symbol: str
    side: Literal["long", "short"]
    limit_px: float
    sl: float
    risk_px: float
    created_i: int
    expire_i: int
    atr15: float
    confirm_mid: float


@dataclass
class Position:
    symbol: str
    side: Literal["long", "short"]
    qty: float
    entry: float
    sl: float
    risk_px: float
    open_i: int
    open_ts: int
    mode: str
    remaining_frac: float = 1.0
    be_armed: bool = False
    trail_atr_mult: float = 2.0
    realized: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    slip_cost: float = 0.0
    entry_fee_mode: str = "maker"


@dataclass
class Trade:
    symbol: str
    side: str
    mode: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float
    fees: float
    funding: float
    slip_cost: float
    exit_reason: str
    entry_mode: str


@dataclass
class Book:
    pending: list[PendingLimit] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    limits_created: int = 0
    limits_filled: int = 0
    equity_curve: list[tuple[int, float]] = field(default_factory=list)


def load_ohlc(symbol: str, tf: str) -> pd.DataFrame:
    pq = HIST / f"{symbol}_{tf}.parquet"
    csv = HIST / f"{symbol}_{tf}.csv"
    if pq.exists():
        df = pd.read_parquet(pq)
    else:
        df = pd.read_csv(csv)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df


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
    df = df.sort_values("fundingRateTimestamp").drop_duplicates("fundingRateTimestamp")
    return df.reset_index(drop=True)


def build_features(symbol: str) -> pd.DataFrame:
    m15 = load_ohlc(symbol, "15m").copy()
    h1 = load_ohlc(symbol, "1h").copy()
    if m15.empty or h1.empty:
        return pd.DataFrame()

    # ATR% on 15m → absolute ATR ≈ atr_pct/100 * close
    atr_pct = atr_pct_series(m15[["ts", "open", "high", "low", "close"]].values.tolist())
    # atr series shorter by 1+period; align to end
    atr_abs = [float("nan")] * len(m15)
    # rebuild properly
    closes = m15["close"].astype(float).tolist()
    highs = m15["high"].astype(float).tolist()
    lows = m15["low"].astype(float).tolist()
    period = 14
    trs = []
    for i in range(1, len(m15)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr_vals = [float("nan")] * len(m15)
    if len(trs) >= period:
        atr = sum(trs[:period]) / period
        # trs[0] corresponds to bar index 1
        atr_vals[period] = atr  # first ATR at index period
        for j in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[j]) / period
            atr_vals[j + 1] = atr
    m15["atr15"] = atr_vals
    m15["ema20_15"] = ema(closes, 20)
    m15["ema50_15"] = ema(closes, 50)

    # 1h EMA without look-ahead: compute on 1h, join by close_ts
    h_closes = h1["close"].astype(float).tolist()
    h1 = h1.copy()
    h1["ema20"] = ema(h_closes, 20)
    h1["ema50"] = ema(h_closes, 50)
    # ATR 1h for BTC filter
    h_vals = h1[["ts", "open", "high", "low", "close"]].values.tolist()
    # absolute ATR 1h
    h_atr = [float("nan")] * len(h1)
    h_hi = h1["high"].astype(float).tolist()
    h_lo = h1["low"].astype(float).tolist()
    h_c = h1["close"].astype(float).tolist()
    h_trs = []
    for i in range(1, len(h1)):
        h_trs.append(
            max(h_hi[i] - h_lo[i], abs(h_hi[i] - h_c[i - 1]), abs(h_lo[i] - h_c[i - 1]))
        )
    if len(h_trs) >= period:
        a = sum(h_trs[:period]) / period
        h_atr[period] = a
        for j in range(period, len(h_trs)):
            a = (a * (period - 1) + h_trs[j]) / period
            h_atr[j + 1] = a
    h1["atr1h"] = h_atr
    period_ms = 3_600_000
    h1["close_ts"] = h1["ts"] + period_ms
    right = h1[
        ["close_ts", "ema20", "ema50", "atr1h", "close", "open", "high", "low"]
    ].rename(
        columns={
            "ema20": "ema20_1h",
            "ema50": "ema50_1h",
            "atr1h": "atr1h",
            "close": "h1_close",
            "open": "h1_open",
            "high": "h1_high",
            "low": "h1_low",
        }
    )
    m15 = pd.merge_asof(
        m15.sort_values("ts"),
        right.sort_values("close_ts"),
        left_on="ts",
        right_on="close_ts",
        direction="backward",
    )
    return m15


def signed_qty(side: str, qty: float) -> float:
    return qty if side == "long" else -qty


def apply_fee(notional: float, rate: float) -> float:
    return abs(notional) * rate


def fill_price_market(side: str, px: float, is_exit: bool) -> tuple[float, float]:
    """Return (exec_px, slip_cost_usd_per_unit_notional_fraction)."""
    if side == "long":
        # buy worse
        exec_px = px * (1 + SLIP) if not is_exit else px * (1 - SLIP)
        # wait: exit long = sell → slip down; exit short = buy → slip up
    if side == "long" and not is_exit:
        exec_px = px * (1 + SLIP)
    elif side == "long" and is_exit:
        exec_px = px * (1 - SLIP)
    elif side == "short" and not is_exit:
        exec_px = px * (1 - SLIP)
    else:  # short exit = buy
        exec_px = px * (1 + SLIP)
    return exec_px, abs(exec_px - px) / px


def limit_touched(side: str, limit_px: float, bar: pd.Series) -> bool:
    """Следующая (текущая при проверке) свеча прошла через цену ордера."""
    lo, hi = float(bar["low"]), float(bar["high"])
    if side == "long":
        return lo <= limit_px  # buy limit fills if traded <= limit
    return hi >= limit_px


def local_extremum(df: pd.DataFrame, i: int, side: str, lookback: int = 5) -> float:
    a = max(0, i - lookback)
    window = df.iloc[a : i + 1]
    if side == "long":
        return float(window["low"].min())
    return float(window["high"].max())


def open_interest_count(book: Book) -> int:
    return len(book.positions)


def side_count(book: Book, side: str) -> int:
    return sum(1 for p in book.positions if p.side == side)


def try_signal(
    df: pd.DataFrame,
    i: int,
    btc_df: pd.DataFrame | None,
    btc_filter: bool,
) -> PendingLimit | None:
    if i < 60:
        return None
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    atr = float(row["atr15"])
    e20 = float(row["ema20_15"])
    e20_1h = float(row["ema20_1h"]) if not pd.isna(row["ema20_1h"]) else float("nan")
    e50_1h = float(row["ema50_1h"]) if not pd.isna(row["ema50_1h"]) else float("nan")
    if math.isnan(atr) or atr <= 0 or math.isnan(e20_1h) or math.isnan(e50_1h):
        return None

    trend_long = e20_1h > e50_1h
    trend_short = e20_1h < e50_1h
    if not trend_long and not trend_short:
        return None

    # BTC filter on same timestamp (btc features already no look-ahead)
    if btc_filter and btc_df is not None:
        # align by ts
        ts = int(row["ts"])
        b = btc_df[btc_df["ts"] <= ts].tail(1)
        if not b.empty:
            br = b.iloc[0]
            h1c = float(br.get("h1_close", float("nan")))
            h1o = float(br.get("h1_open", float("nan")))
            atr_b = float(br.get("atr1h", float("nan")))
            if not math.isnan(h1c) and not math.isnan(h1o) and not math.isnan(atr_b) and atr_b > 0:
                move = h1c - h1o
                if trend_long and move < -1.5 * atr_b:
                    return None
                if trend_short and move > 1.5 * atr_b:
                    return None

    o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    # pullback near EMA20_15: wick or close touched band ±0.15*ATR
    band = 0.15 * atr
    touched_ema = (low <= e20 + band) and (h >= e20 - band)

    # confirming candle in trend direction (bullish/bearish close)
    if trend_long:
        if not touched_ema:
            return None
        bull = c > o and c > e20
        if not bull:
            return None
        side: Literal["long", "short"] = "long"
        limit_px = (o + c) / 2.0  # mid of confirm candle
        # also allow open as alt — use mid as specified
        ext = local_extremum(df, i, "long")
        sl = ext - 0.5 * atr
        if sl >= limit_px:
            sl = limit_px - 0.5 * atr
        risk = limit_px - sl
    else:
        if not touched_ema:
            return None
        bear = c < o and c < e20
        if not bear:
            return None
        side = "short"
        limit_px = (o + c) / 2.0
        ext = local_extremum(df, i, "short")
        sl = ext + 0.5 * atr
        if sl <= limit_px:
            sl = limit_px + 0.5 * atr
        risk = sl - limit_px

    if risk <= 0 or risk / limit_px < 1e-6:
        return None

    return PendingLimit(
        symbol=str(df.attrs.get("symbol", "")),
        side=side,
        limit_px=limit_px,
        sl=sl,
        risk_px=risk,
        created_i=i,
        expire_i=i + LIMIT_TTL,
        atr15=atr,
        confirm_mid=limit_px,
    )


def close_position(
    pos: Position,
    exit_px_raw: float,
    ts: int,
    reason: str,
    frac: float,
    book: Book,
    fee_rate: float,
    use_slip: bool,
) -> None:
    frac = min(frac, pos.remaining_frac)
    if frac <= 0:
        return
    qty = pos.qty * frac
    if use_slip:
        exit_px, slip_frac = fill_price_market(pos.side, exit_px_raw, is_exit=True)
        slip_cost = abs(exit_px_raw) * qty * slip_frac
    else:
        # maker-like exit unused in v1 exits (stops/TP market)
        exit_px, slip_cost = exit_px_raw, 0.0
    notional = exit_px * qty
    fee = apply_fee(notional, fee_rate)
    if pos.side == "long":
        pnl = (exit_px - pos.entry) * qty
    else:
        pnl = (pos.entry - exit_px) * qty
    pnl_net = pnl - fee - slip_cost
    # allocate proportional funding already on pos? funding tracked on pos separately
    pos.fees += fee
    pos.slip_cost += slip_cost
    pos.realized += pnl_net
    pos.remaining_frac -= frac

    r_mult = (pnl_net) / (pos.risk_px * pos.qty) if pos.risk_px * pos.qty > 0 else 0.0
    # For partial, scale risk by frac in R report for this slice
    r_slice = pnl_net / (pos.risk_px * qty) if pos.risk_px * qty > 0 else 0.0

    # remaining уже уменьшен на frac; funding делим пропорционально
    orig_rem = pos.remaining_frac + frac
    if pos.remaining_frac <= 1e-12:
        fund_share = pos.funding
        pos.funding = 0.0
    else:
        fund_share = pos.funding * (frac / orig_rem) if orig_rem > 0 else 0.0
        pos.funding -= fund_share
    # funding уже сидит в pos.realized через apply_funding — в trade.pnl кладём
    # только pnl_net (цена+fee+slip); funding отдельным полем для разбивки издержек
    book.trades.append(
        Trade(
            symbol=pos.symbol,
            side=pos.side,
            mode=pos.mode,
            entry_ts=pos.open_ts,
            exit_ts=ts,
            entry=pos.entry,
            exit=exit_px,
            qty=qty,
            pnl=pnl_net + fund_share,
            r_multiple=(pnl_net + fund_share) / (pos.risk_px * qty) if pos.risk_px * qty > 0 else 0.0,
            fees=fee,
            funding=fund_share,
            slip_cost=slip_cost,
            exit_reason=reason,
            entry_mode=pos.entry_fee_mode,
        )
    )


def apply_funding(pos: Position, rate: float) -> float:
    """Bybit: funding = -position_value * rate for long if rate>0 (longs pay)."""
    notional = pos.entry * pos.qty * pos.remaining_frac  # approx mark≈entry for v1
    # long pays positive funding
    if pos.side == "long":
        cash = -notional * rate
    else:
        cash = notional * rate
    pos.funding += cash
    pos.realized += cash
    return cash


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    funding: pd.DataFrame,
    btc_df: pd.DataFrame | None,
    *,
    mode: str,
    btc_filter: bool,
    entry_mode: str,
    book: Book,
) -> None:
    df = df.copy()
    df.attrs["symbol"] = symbol
    fund_idx = 0
    fund_ts = (
        funding["fundingRateTimestamp"].astype("int64").tolist() if not funding.empty else []
    )
    fund_rate = funding["fundingRate"].astype(float).tolist() if not funding.empty else []

    for i in range(len(df)):
        row = df.iloc[i]
        ts = int(row["ts"])
        o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
        atr = float(row["atr15"]) if not pd.isna(row["atr15"]) else float("nan")

        # funding events between previous bar and now (inclusive at ts)
        while fund_idx < len(fund_ts) and fund_ts[fund_idx] <= ts:
            rate = fund_rate[fund_idx]
            for pos in book.positions:
                if pos.symbol == symbol and pos.remaining_frac > 0:
                    apply_funding(pos, rate)
            fund_idx += 1

        # manage positions
        still: list[Position] = []
        for pos in book.positions:
            if pos.symbol != symbol:
                still.append(pos)
                continue
            if pos.remaining_frac <= 1e-12:
                continue

            hit_sl = (pos.side == "long" and low <= pos.sl) or (
                pos.side == "short" and h >= pos.sl
            )
            # TP levels
            if pos.side == "long":
                r = pos.risk_px
                tp_full = pos.entry + 2.0 * r
                tp_partial = pos.entry + 1.2 * r
            else:
                r = pos.risk_px
                tp_full = pos.entry - 2.0 * r
                tp_partial = pos.entry - 1.2 * r

            if mode == "A":
                hit_tp = (pos.side == "long" and h >= tp_full) or (
                    pos.side == "short" and low <= tp_full
                )
                # conservative: if both SL and TP in same bar → SL first
                if hit_sl:
                    close_position(pos, pos.sl, ts, "sl", pos.remaining_frac, book, TAKER, True)
                elif hit_tp:
                    close_position(pos, tp_full, ts, "tp2R", pos.remaining_frac, book, TAKER, True)
                else:
                    still.append(pos)
            else:  # mode B
                if hit_sl and not pos.be_armed:
                    close_position(pos, pos.sl, ts, "sl", pos.remaining_frac, book, TAKER, True)
                    continue
                if hit_sl and pos.be_armed:
                    close_position(pos, pos.sl, ts, "be_sl", pos.remaining_frac, book, TAKER, True)
                    continue
                # partial TP
                hit_part = (pos.side == "long" and h >= tp_partial) or (
                    pos.side == "short" and low <= tp_partial
                )
                if hit_part and not pos.be_armed and pos.remaining_frac > 0.5:
                    close_position(pos, tp_partial, ts, "tp1.2R_half", 0.5, book, TAKER, True)
                    pos.be_armed = True
                    pos.sl = pos.entry  # BE
                # trail remainder
                if pos.be_armed and pos.remaining_frac > 1e-12 and not math.isnan(atr):
                    if pos.side == "long":
                        trail = c - pos.trail_atr_mult * atr
                        pos.sl = max(pos.sl, trail)
                        if low <= pos.sl:
                            close_position(
                                pos, pos.sl, ts, "trail", pos.remaining_frac, book, TAKER, True
                            )
                            continue
                    else:
                        trail = c + pos.trail_atr_mult * atr
                        pos.sl = min(pos.sl, trail)
                        if h >= pos.sl:
                            close_position(
                                pos, pos.sl, ts, "trail", pos.remaining_frac, book, TAKER, True
                            )
                            continue
                if pos.remaining_frac > 1e-12:
                    still.append(pos)
        book.positions = [p for p in still if p.remaining_frac > 1e-12] + [
            p for p in book.positions if p.symbol != symbol and p in still
        ]
        # fix positions list properly
        book.positions = [p for p in still if p.remaining_frac > 1e-12]

        # pending limits — check fill on this bar (bar after signal)
        new_pending: list[PendingLimit] = []
        for pend in book.pending:
            if pend.symbol != symbol:
                new_pending.append(pend)
                continue
            if i > pend.expire_i:
                continue  # expired
            if i <= pend.created_i:
                new_pending.append(pend)
                continue  # start checking from next bar
            # capacity
            if open_interest_count(book) >= MAX_POS:
                new_pending.append(pend)
                continue
            if side_count(book, pend.side) >= MAX_POS:
                new_pending.append(pend)
                continue
            # already in symbol?
            if any(p.symbol == symbol for p in book.positions):
                new_pending.append(pend)
                continue

            filled = False
            entry_px = pend.limit_px
            fee_mode = "maker"
            fee_rate = MAKER
            if entry_mode == "maker":
                if limit_touched(pend.side, pend.limit_px, row):
                    filled = True
                    entry_px = pend.limit_px
            else:
                # taker sensitivity: enter at next open with slip
                if i == pend.created_i + 1:
                    filled = True
                    entry_px, _ = fill_price_market(pend.side, o, is_exit=False)
                    fee_mode = "taker"
                    fee_rate = TAKER

            if filled:
                book.limits_filled += 1
                qty = LEG_USD / entry_px
                fee = apply_fee(entry_px * qty, fee_rate)
                slip_cost = 0.0
                if fee_mode == "taker":
                    slip_cost = abs(entry_px - o) * qty
                pos = Position(
                    symbol=symbol,
                    side=pend.side,
                    qty=qty,
                    entry=entry_px,
                    sl=pend.sl,
                    risk_px=pend.risk_px,
                    open_i=i,
                    open_ts=ts,
                    mode=mode,
                    fees=fee,
                    slip_cost=slip_cost,
                    entry_fee_mode=fee_mode,
                    realized=-fee - slip_cost,
                )
                book.positions.append(pos)
            else:
                if i < pend.expire_i:
                    new_pending.append(pend)
        book.pending = [p for p in book.pending if p.symbol != symbol] + new_pending

        # new signals on bar close (order lives for next bars)
        if open_interest_count(book) < MAX_POS and not any(
            p.symbol == symbol for p in book.positions
        ):
            if not any(p.symbol == symbol for p in book.pending):
                sig = try_signal(df, i, btc_df, btc_filter)
                if sig is not None:
                    sig.symbol = symbol
                    # respect side cap
                    if side_count(book, sig.side) < MAX_POS:
                        book.pending.append(sig)
                        book.limits_created += 1

        # equity mark
        upnl = 0.0
        for pos in book.positions:
            if pos.symbol != symbol:
                continue
            if pos.side == "long":
                upnl += (c - pos.entry) * pos.qty * pos.remaining_frac
            else:
                upnl += (pos.entry - c) * pos.qty * pos.remaining_frac
            upnl += pos.realized  # already includes closed partials & fees on this pos
        # simpler: track realized trades only in report; skip complex equity here


def summarize(trades: list[Trade], limits_created: int, limits_filled: int) -> dict:
    if not trades:
        return {
            "n": 0,
            "fill_rate": limits_filled / limits_created if limits_created else 0.0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "pf": 0.0,
            "exp_r": 0.0,
            "exp_usd": 0.0,
            "max_dd_usd": 0.0,
            "max_dd_pct": 0.0,
            "fees": 0.0,
            "funding": 0.0,
            "slip": 0.0,
            "pnl": 0.0,
        }
    pnls = [t.pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # equity curve / DD
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    start_cap = 160.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    max_dd_usd = abs(max_dd)
    max_dd_pct = max_dd_usd / start_cap * 100.0
    return {
        "n": len(trades),
        "fill_rate": limits_filled / limits_created if limits_created else 0.0,
        "win_rate": len(wins) / len(pnls),
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "avg_win_r": float(np.mean(win_r)) if win_r else 0.0,
        "avg_loss_r": float(np.mean(loss_r)) if loss_r else 0.0,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "exp_r": float(np.mean(rs)),
        "exp_usd": float(np.mean(pnls)),
        "max_dd_usd": max_dd_usd,
        "max_dd_pct": max_dd_pct,
        "fees": sum(t.fees for t in trades),
        "funding": sum(t.funding for t in trades),
        "slip": sum(t.slip_cost for t in trades),
        "pnl": sum(pnls),
    }


def run_mode(
    mode: str,
    features: dict[str, pd.DataFrame],
    fundings: dict[str, pd.DataFrame],
    btc_df: pd.DataFrame | None,
    btc_filter: bool,
    entry_mode: str,
) -> tuple[Book, dict]:
    book = Book()
    # Interleave by walking each symbol independently but shared MAX_POS
    # Practical v1: sequential symbols share book constraints — process in lockstep by ts union
    all_ts = sorted(set().union(*[set(df["ts"].tolist()) for df in features.values() if len(df)]))
    # Build index maps
    idx = {sym: {int(r.ts): i for i, r in enumerate(df.itertuples())} for sym, df in features.items()}
    pointers = {sym: 0 for sym in features}
    pending_check_done = set()

    # Simpler approach: run each symbol separately with own max 2, then note portfolio
    # User asked portfolio max 2 — do lockstep.

    fund_i = {sym: 0 for sym in features}

    for ts in all_ts:
        # 1) funding
        for sym, df in features.items():
            funding = fundings[sym]
            if funding.empty:
                continue
            fi = fund_i[sym]
            fts = funding["fundingRateTimestamp"].astype("int64")
            while fi < len(fts) and int(fts.iloc[fi]) <= ts:
                rate = float(funding["fundingRate"].iloc[fi])
                for pos in book.positions:
                    if pos.symbol == sym and pos.remaining_frac > 0:
                        cash = apply_funding(pos, rate)
                        # attach to last trade later; keep on pos
                fi += 1
            fund_i[sym] = fi

        # 2) per symbol bar events if this ts is a bar open for symbol
        for sym, df in features.items():
            i = idx[sym].get(ts)
            if i is None:
                continue
            # reuse per-bar logic by calling a thin wrapper — extract one bar update
            _bar_update(sym, df, i, book, mode, btc_df, btc_filter, entry_mode)

        # mark equity
        mark = sum(t.pnl for t in book.trades)
        for pos in book.positions:
            # find close
            i = idx[pos.symbol].get(ts)
            if i is None:
                continue
            c = float(features[pos.symbol].iloc[i]["close"])
            if pos.side == "long":
                mark += (c - pos.entry) * pos.qty * pos.remaining_frac + pos.realized
            else:
                mark += (pos.entry - c) * pos.qty * pos.remaining_frac + pos.realized
        book.equity_curve.append((ts, mark))

    # force close leftovers at last price
    for pos in list(book.positions):
        df = features[pos.symbol]
        last = df.iloc[-1]
        close_position(
            pos,
            float(last["close"]),
            int(last["ts"]),
            "eod",
            pos.remaining_frac,
            book,
            TAKER,
            True,
        )
    book.positions = []

    summary = summarize(book.trades, book.limits_created, book.limits_filled)
    return book, summary


def _bar_update(
    symbol: str,
    df: pd.DataFrame,
    i: int,
    book: Book,
    mode: str,
    btc_df: pd.DataFrame | None,
    btc_filter: bool,
    entry_mode: str,
) -> None:
    row = df.iloc[i]
    ts = int(row["ts"])
    o, h, low, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    atr = float(row["atr15"]) if not pd.isna(row["atr15"]) else float("nan")

    # positions
    still: list[Position] = []
    for pos in book.positions:
        if pos.symbol != symbol:
            still.append(pos)
            continue
        if pos.remaining_frac <= 1e-12:
            continue
        hit_sl = (pos.side == "long" and low <= pos.sl) or (pos.side == "short" and h >= pos.sl)
        r = pos.risk_px
        if pos.side == "long":
            tp_full = pos.entry + 2.0 * r
            tp_partial = pos.entry + 1.2 * r
        else:
            tp_full = pos.entry - 2.0 * r
            tp_partial = pos.entry - 1.2 * r

        if mode == "A":
            hit_tp = (pos.side == "long" and h >= tp_full) or (
                pos.side == "short" and low <= tp_full
            )
            if hit_sl:
                close_position(pos, pos.sl, ts, "sl", pos.remaining_frac, book, TAKER, True)
            elif hit_tp:
                close_position(pos, tp_full, ts, "tp2R", pos.remaining_frac, book, TAKER, True)
            else:
                still.append(pos)
        else:
            if hit_sl:
                close_position(
                    pos,
                    pos.sl,
                    ts,
                    "be_sl" if pos.be_armed else "sl",
                    pos.remaining_frac,
                    book,
                    TAKER,
                    True,
                )
                continue
            hit_part = (pos.side == "long" and h >= tp_partial) or (
                pos.side == "short" and low <= tp_partial
            )
            if hit_part and not pos.be_armed and pos.remaining_frac > 0.5 - 1e-9:
                close_position(pos, tp_partial, ts, "tp1.2R_half", 0.5, book, TAKER, True)
                pos.be_armed = True
                pos.sl = pos.entry
            if pos.be_armed and pos.remaining_frac > 1e-12 and not math.isnan(atr):
                if pos.side == "long":
                    pos.sl = max(pos.sl, c - 2.0 * atr)
                    if low <= pos.sl:
                        close_position(
                            pos, pos.sl, ts, "trail", pos.remaining_frac, book, TAKER, True
                        )
                        continue
                else:
                    pos.sl = min(pos.sl, c + 2.0 * atr)
                    if h >= pos.sl:
                        close_position(
                            pos, pos.sl, ts, "trail", pos.remaining_frac, book, TAKER, True
                        )
                        continue
            if pos.remaining_frac > 1e-12:
                still.append(pos)
    book.positions = still

    # pending fills
    kept_pending: list[PendingLimit] = []
    for pend in book.pending:
        if pend.symbol != symbol:
            kept_pending.append(pend)
            continue
        if i > pend.expire_i:
            continue
        if i <= pend.created_i:
            kept_pending.append(pend)
            continue
        if open_interest_count(book) >= MAX_POS:
            kept_pending.append(pend)
            continue
        if any(p.symbol == symbol for p in book.positions):
            kept_pending.append(pend)
            continue

        filled = False
        entry_px = pend.limit_px
        fee_mode = "maker"
        fee_rate = MAKER
        slip_cost = 0.0
        if entry_mode == "maker":
            if limit_touched(pend.side, pend.limit_px, row):
                filled = True
        else:
            if i == pend.created_i + 1:
                filled = True
                entry_px, slip_frac = fill_price_market(pend.side, o, is_exit=False)
                fee_mode = "taker"
                fee_rate = TAKER
                slip_cost = abs(entry_px - o) * (LEG_USD / entry_px)

        if filled:
            book.limits_filled += 1
            qty = LEG_USD / entry_px
            fee = apply_fee(entry_px * qty, fee_rate)
            book.positions.append(
                Position(
                    symbol=symbol,
                    side=pend.side,
                    qty=qty,
                    entry=entry_px,
                    sl=pend.sl,
                    risk_px=abs(pend.risk_px),
                    open_i=i,
                    open_ts=ts,
                    mode=mode,
                    fees=fee,
                    slip_cost=slip_cost,
                    entry_fee_mode=fee_mode,
                    realized=-fee - slip_cost,
                )
            )
        elif i < pend.expire_i:
            kept_pending.append(pend)
    book.pending = kept_pending

    # signals
    if open_interest_count(book) < MAX_POS and not any(p.symbol == symbol for p in book.positions):
        if not any(p.symbol == symbol for p in book.pending):
            sig = try_signal(df, i, btc_df, btc_filter)
            if sig is not None:
                sig.symbol = symbol
                book.pending.append(sig)
                book.limits_created += 1


def print_report(title: str, summary: dict, by_sym: dict[str, dict]) -> None:
    print(f"\n=== {title} ===")
    print(
        f"n={summary['n']} fill_rate={summary['fill_rate']:.1%} WR={summary['win_rate']:.1%} "
        f"PF={summary['pf']:.2f} Exp$={summary['exp_usd']:.3f} ExpR={summary['exp_r']:.3f} "
        f"PnL=${summary['pnl']:.2f} MaxDD=${summary['max_dd_usd']:.2f} ({summary['max_dd_pct']:.1f}%)"
    )
    print(
        f"costs: fees=${summary['fees']:.2f} funding=${summary['funding']:.2f} slip=${summary['slip']:.2f}"
    )
    print(
        f"avgWin=${summary['avg_win']:.3f} avgLoss=${summary['avg_loss']:.3f} "
        f"avgWinR={summary['avg_win_r']:.2f} avgLossR={summary['avg_loss_r']:.2f}"
    )
    for sym, s in by_sym.items():
        print(
            f"  {sym}: n={s['n']} WR={s['win_rate']:.1%} PF={s['pf']:.2f} "
            f"ExpR={s['exp_r']:.3f} PnL=${s['pnl']:.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--btc-filter", action="store_true")
    ap.add_argument("--entry-mode", choices=["maker", "taker"], default="maker")
    ap.add_argument("--symbols", nargs="*", default=TRADE_SYMBOLS)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    print("Building features…", flush=True)
    features = {}
    fundings = {}
    for sym in args.symbols + [BTC]:
        print(f"  {sym}", flush=True)
        features[sym] = build_features(sym)
        fundings[sym] = load_funding(sym)
        print(f"    15m bars={len(features[sym])} funding={len(fundings[sym])}", flush=True)

    btc_df = features.get(BTC)
    trade_feats = {s: features[s] for s in args.symbols if s in features and len(features[s])}

    results = {}
    for mode in ("A", "B"):
        book, summary = run_mode(
            mode, trade_feats, fundings, btc_df, args.btc_filter, args.entry_mode
        )
        by_sym = {}
        for sym in args.symbols:
            tr = [t for t in book.trades if t.symbol == sym]
            by_sym[sym] = summarize(tr, book.limits_created, book.limits_filled)
        print_report(
            f"Mode {mode} | entry={args.entry_mode} | btc_filter={args.btc_filter}",
            summary,
            by_sym,
        )
        # save trades
        tdf = pd.DataFrame([asdict(t) for t in book.trades])
        tdf.to_csv(OUT / f"trades_mode{mode}_{args.entry_mode}_btc{int(args.btc_filter)}.csv", index=False)
        results[mode] = {"summary": summary, "by_symbol": by_sym}

    # taker sensitivity if primary was maker
    if args.entry_mode == "maker":
        print("\n--- Sensitivity: taker entry ---", flush=True)
        for mode in ("A", "B"):
            book, summary = run_mode(mode, trade_feats, fundings, btc_df, args.btc_filter, "taker")
            by_sym = {
                sym: summarize([t for t in book.trades if t.symbol == sym], book.limits_created, book.limits_filled)
                for sym in args.symbols
            }
            print_report(f"Mode {mode} | entry=taker | btc_filter={args.btc_filter}", summary, by_sym)
            results[f"{mode}_taker"] = {"summary": summary, "by_symbol": by_sym}

    out_json = OUT / f"report_btc{int(args.btc_filter)}.json"
    out_json.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nReport → {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
