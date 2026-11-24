#!/usr/bin/env python3
"""Parity: research backtest path vs skills.crypto.linear.strategy_core на тех же OHLC.

Прогоняет Core-2 S0 (+H1+H2+TP2.5R, rolling_4320) двумя путями фичей/сигнала
и сверяет список (symbol, side, bar_ts, limit, sl, tp).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from research_linear_diag_ablation_v2 import (
    Cfg,
    build_features as research_build,
    load_ohlc,
    try_signal,
)
from skills.crypto.linear.strategy_core import (
    StrategyParams,
    build_features as core_build,
    signal_at_bar,
)

HIST = Path("research_linear_data/history")
SYMBOLS = ["ZECUSDT", "HYPEUSDT"]
BTC = "BTCUSDT"


def main() -> int:
    params = StrategyParams(er4h_median_mode="rolling_4320", tp_r=2.5)
    cfg = Cfg(
        name="parity",
        btc_filter=True,
        h1a_4h=True,
        h1b_er4h=True,
        er4h_median_mode="rolling_4320",
        h2_impulse=True,
        h2_min_sl_atr=1.5,
        exit_mode="tp2_5r",
        tp_r=2.5,
    )

    # research features from parquet
    research = {}
    for sym in SYMBOLS + [BTC]:
        research[sym] = research_build(sym)

    # core features from same parquet frames
    core = {}
    for sym in SYMBOLS + [BTC]:
        m15 = load_ohlc(sym, "15m")
        h1 = load_ohlc(sym, "1h")
        h4 = load_ohlc(sym, "4h")
        core[sym] = core_build(m15, h1, h4)

    mismatches = []
    n_core = n_research = 0
    for sym in SYMBOLS:
        df_r = research[sym]
        df_c = core[sym]
        btc_r = research[BTC]
        btc_c = core[BTC]
        # align by ts
        ts_c = {int(t): i for i, t in enumerate(df_c["ts"].astype("int64"))}
        for i in range(len(df_r)):
            ts = int(df_r.iloc[i]["ts"])
            j = ts_c.get(ts)
            if j is None:
                continue
            sr = try_signal(df_r, i, btc_r, cfg)
            sc = signal_at_bar(sym, df_c, j, btc_c, params)
            if sr:
                n_research += 1
            if sc:
                n_core += 1
            if (sr is None) != (sc is None):
                mismatches.append((sym, ts, "presence", bool(sr), bool(sc)))
                continue
            if sr and sc:
                for name, a, b in (
                    ("limit", sr.limit_px, sc.limit_px),
                    ("sl", sr.sl, sc.sl),
                    ("side", sr.side, sc.side),
                ):
                    if name == "side":
                        if a != b:
                            mismatches.append((sym, ts, name, a, b))
                    elif abs(float(a) - float(b)) > 1e-8 * max(1.0, abs(float(a))):
                        mismatches.append((sym, ts, name, a, b))

    print(f"research_signals={n_research} core_signals={n_core} mismatches={len(mismatches)}")
    for row in mismatches[:20]:
        print(" ", row)
    ok = len(mismatches) == 0 and n_research == n_core
    print("PARITY", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
