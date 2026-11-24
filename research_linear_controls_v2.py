#!/usr/bin/env python3
"""
3 контрольные проверки для +H1+H2+TP2.5R (правила сигналов не меняем).

1) Состав портфеля Core-2 / Core-3a / Core-3b на S0 и S1
2) Risk-clamped leg [$25..$50] vs фикс $40
3) Expanding median er_4h vs rolling 4320 / 2880

Запуск:
  .venv/bin/python research_linear_controls_v2.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research_linear_diag_ablation_v2 import (
    Cfg,
    Trade,
    build_features,
    load_funding,
    load_ohlc,
    run,
    summarize_trades,
)
from research_linear_stress_oos_v2 import best_cfg, max_dd_from_trades

OUT = Path("research_linear_data/controls_v2")
BTC = "BTCUSDT"


def metrics(res: dict, n_ts: int) -> dict:
    trades: list[Trade] = res["trades"]
    g = res["global_stats"]
    s = summarize_trades(trades, g.orders, g.fills)
    dd, _ = max_dd_from_trades(trades)
    full = int(res.get("max_pos_full_bars") or 0)
    return {
        "fills": g.fills,
        "fill_rate": g.fills / g.orders if g.orders else 0.0,
        "WR": s["WR"],
        "PF": s["PF"],
        "Exp_R": s["Exp_R"],
        "Exp_usd": s["Exp_usd"],
        "PnL": s["PnL"],
        "MaxDD": dd,
        "full_2_2_pct": full / n_ts * 100.0 if n_ts else 0.0,
        "orders": g.orders,
        "n_trades": len(trades),
    }


def by_sym_legs(trades: list[Trade]) -> dict[str, dict]:
    out = {}
    for sym in sorted({t.symbol for t in trades}):
        tr = [t for t in trades if t.symbol == sym]
        legs = [t.leg_usd for t in tr if t.leg_usd > 0]
        out[sym] = {
            "n": len(tr),
            "pnl": sum(t.pnl for t in tr),
            "avg_leg": float(np.mean(legs)) if legs else 0.0,
            "med_leg": float(np.median(legs)) if legs else 0.0,
            "avg_sl_pct": float(np.mean([t.sl_pct for t in tr])) if tr else 0.0,
        }
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# Controls v2: +H1+H2+TP2.5R\n"]

    need = ["ZECUSDT", "HYPEUSDT", "SOLUSDT", "AAVEUSDT", BTC]
    print("Building features…", flush=True)
    feats = {}
    funds = {}
    for sym in need:
        feats[sym] = build_features(sym)
        funds[sym] = load_funding(sym)
        print(f"  {sym}: {len(feats[sym])}", flush=True)
    btc_df = feats[BTC]

    portfolios = {
        "Core-2": ["ZECUSDT", "HYPEUSDT"],
        "Core-3a": ["ZECUSDT", "HYPEUSDT", "SOLUSDT"],
        "Core-3b": ["ZECUSDT", "HYPEUSDT", "AAVEUSDT"],
    }

    # --- 1. Portfolio composition S0 / S1 ---
    lines.append("## 1. Состав портфеля (S0 vs S1)\n")
    lines.append(
        "Core-3b = ZEC+HYPE+AAVE (AAVE ATR15≈0.60%, corr_sol≈0.79 vs SOL corr=1.0 / ATR15≈0.44).\n"
    )
    lines.append(
        "| portfolio | scenario | fills | WR | PF | Exp_R | PnL | MaxDD | 2/2 bars % |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    rows1 = []
    for pname, syms in portfolios.items():
        pf = {s: feats[s] for s in syms}
        fd = {s: funds[s] for s in syms}
        n_ts = len(sorted(set().union(*[set(df["ts"].astype("int64")) for df in pf.values()])))
        for scen, through in (("S0", 0.0), ("S1", 0.0002)):
            cfg = best_cfg(
                name=f"{pname}_{scen}",
                fill_through_pct=through,
                er4h_median_mode="expanding",
                leg_usd=40.0,
                risk_clamp=False,
            )
            print(f"1) {pname} {scen}…", flush=True)
            res = run(cfg, pf, fd, btc_df)
            m = metrics(res, n_ts)
            lines.append(
                f"| {pname} | {scen} | {m['fills']} | {m['WR']:.1%} | {m['PF']:.2f} | "
                f"{m['Exp_R']:.3f} | {m['PnL']:.2f} | {m['MaxDD']:.2f} | {m['full_2_2_pct']:.1f}% |"
            )
            rows1.append({"portfolio": pname, "scenario": scen, **m})

    # --- 2. Risk-clamped sizing on Core-3a S0 ---
    lines.append("\n## 2. Risk-clamped leg [$25..$50] vs фикс $40 (Core-3a, S0)\n")
    lines.append(
        "Формула: `leg_usd = clip(0.65 / sl_pct, 25, 50)`, затем `max(leg, min_usd)` "
        "(min_usd по контрактам ≪ $25 → клип доминирует).\n"
    )
    lines.append("| sizing | fills | WR | PF | Exp_R | PnL | MaxDD | 2/2 % |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    pf3 = {s: feats[s] for s in portfolios["Core-3a"]}
    fd3 = {s: funds[s] for s in portfolios["Core-3a"]}
    n_ts3 = len(sorted(set().union(*[set(df["ts"].astype("int64")) for df in pf3.values()])))
    rows2 = []
    for label, clamp in (("fixed_$40", False), ("risk_clamp_$0.65", True)):
        cfg = best_cfg(
            name=f"Core3a_S0_{label}",
            fill_through_pct=0.0,
            risk_clamp=clamp,
            target_risk_usd=0.65,
            leg_min_usd=25.0,
            leg_max_usd=50.0,
            leg_usd=40.0,
        )
        print(f"2) {label}…", flush=True)
        res = run(cfg, pf3, fd3, btc_df)
        m = metrics(res, n_ts3)
        lines.append(
            f"| {label} | {m['fills']} | {m['WR']:.1%} | {m['PF']:.2f} | {m['Exp_R']:.3f} | "
            f"{m['PnL']:.2f} | {m['MaxDD']:.2f} | {m['full_2_2_pct']:.1f}% |"
        )
        legs = by_sym_legs(res["trades"])
        rows2.append({"sizing": label, **m, "by_sym": legs})
        lines.append("")
        lines.append(f"### {label} по тикерам")
        lines.append("| symbol | n | PnL | avg_leg | med_leg | avg_sl_pct |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for sym, info in legs.items():
            lines.append(
                f"| {sym} | {info['n']} | {info['pnl']:.2f} | {info['avg_leg']:.1f} | "
                f"{info['med_leg']:.1f} | {info['avg_sl_pct']:.3f}% |"
            )

    # --- 3. er_4h median stationarity ---
    lines.append("\n## 3. Стационарность er_4h: expanding vs rolling (Core-3a, S0)\n")
    lines.append(
        "Rolling: causal `median(4320)` ≈45д / `median(2880)` ≈30д. "
        "Цель — live warm-up 45д REST без расхождения с бэктестом.\n"
    )
    lines.append("| median_mode | fills | WR | PF | Exp_R | PnL | MaxDD | ΔPnL vs expanding |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    rows3 = []
    base_pnl = None
    for mode in ("expanding", "rolling_4320", "rolling_2880"):
        cfg = best_cfg(
            name=f"Core3a_S0_er_{mode}",
            fill_through_pct=0.0,
            er4h_median_mode=mode,
            risk_clamp=False,
            leg_usd=40.0,
        )
        print(f"3) er4h {mode}…", flush=True)
        res = run(cfg, pf3, fd3, btc_df)
        m = metrics(res, n_ts3)
        if base_pnl is None:
            base_pnl = m["PnL"]
        d_pnl = m["PnL"] - base_pnl
        lines.append(
            f"| {mode} | {m['fills']} | {m['WR']:.1%} | {m['PF']:.2f} | {m['Exp_R']:.3f} | "
            f"{m['PnL']:.2f} | {m['MaxDD']:.2f} | {d_pnl:+.2f} |"
        )
        rows3.append({"mode": mode, **m, "d_pnl": d_pnl})

    # threshold overlap sanity
    lines.append("\n### Sanity: доля баров, где rolling_4320 ≈ expanding (±5%)\n")
    for sym in portfolios["Core-3a"]:
        df = feats[sym]
        a = df["er_4h_exp_med"].astype(float)
        b = df["er_4h_roll_4320"].astype(float)
        mask = a.notna() & b.notna() & (a > 1e-9)
        if mask.sum() == 0:
            continue
        rel = (b[mask] - a[mask]).abs() / a[mask]
        pct = float((rel <= 0.05).mean() * 100)
        lines.append(f"- {sym}: {pct:.1f}% баров с |roll−exp|/exp ≤ 5% (n={int(mask.sum())})")

    text = "\n".join(lines) + "\n"
    (OUT / "REPORT.md").write_text(text, encoding="utf-8")
    pd.DataFrame(rows1).to_csv(OUT / "portfolio.csv", index=False)
    # flatten rows2 for csv
    flat2 = []
    for r in rows2:
        base = {k: v for k, v in r.items() if k != "by_sym"}
        for sym, info in r["by_sym"].items():
            flat2.append({**base, "symbol": sym, **info})
    pd.DataFrame(flat2).to_csv(OUT / "risk_clamp.csv", index=False)
    pd.DataFrame(rows3).to_csv(OUT / "er4h_median.csv", index=False)
    print("\n" + text)
    print(f"→ {OUT / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
