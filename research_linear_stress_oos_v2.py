#!/usr/bin/env python3
"""
Стресс-тест + OOS для +H1+H2+TP2.5R (без подгонки порогов).

Запуск:
  .venv/bin/python research_linear_stress_oos_v2.py
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from research_linear.bybit_public import fetch_funding_history, fetch_klines, fnum
from research_linear_diag_ablation_v2 import (
    Cfg,
    Trade,
    build_features,
    load_funding,
    load_ohlc,
    run,
    summarize_trades,
    HIST,
    OUT as _UNUSED,
)

OUT = Path("research_linear_data/stress_oos_v2")
OOS_SYMBOLS = ["AAVEUSDT", "LINKUSDT", "ENAUSDT"]
CORE = ["SOLUSDT", "HYPEUSDT", "ZECUSDT"]
BTC = "BTCUSDT"
BH = ["BTCUSDT", "SOLUSDT", "HYPEUSDT", "ZECUSDT"]


def best_cfg(**kw) -> Cfg:
    base = dict(
        name="H1+H2+TP2.5R",
        btc_filter=True,
        entry_mode="maker",
        h1a_4h=True,
        h1b_er4h=True,
        er4h_fixed=None,  # expanding median
        h2_impulse=True,
        h2_min_sl_atr=1.5,
        h3_entry_near=False,
        ttl=2,
        exit_mode="tp2_5r",
        tp_r=2.5,
        fill_through_pct=0.0,
        maker_fee=0.0002,
        taker_fee=0.00055,
        slip_exit=0.0002,
        slip_entry=0.0002,
    )
    base.update(kw)
    return Cfg(**base)


def download_symbol(sym: str, days: int = 90) -> None:
    HIST.mkdir(parents=True, exist_ok=True)
    print(f"download {sym}…", flush=True)
    for iv, name in (("15", "15m"), ("60", "1h"), ("240", "4h")):
        rows = fetch_klines(sym, iv, days)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        for c in ("open", "high", "low", "close", "volume", "turnover"):
            df[c] = df[c].map(fnum)
        df["ts"] = df["ts"].astype("int64")
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
        df.to_csv(HIST / f"{sym}_{name}.csv", index=False)
        df.to_parquet(HIST / f"{sym}_{name}.parquet", index=False)
        print(f"  {name}: {len(df)}", flush=True)
        time.sleep(0.05)
    fund = fetch_funding_history(sym, days)
    fdf = pd.DataFrame(fund)
    if not fdf.empty:
        fdf["fundingRate"] = fdf["fundingRate"].map(fnum)
        fdf["fundingRateTimestamp"] = fdf["fundingRateTimestamp"].astype("int64")
        fdf["dt"] = pd.to_datetime(fdf["fundingRateTimestamp"], unit="ms", utc=True)
        fdf = fdf.sort_values("fundingRateTimestamp").drop_duplicates("fundingRateTimestamp")
    else:
        fdf = pd.DataFrame(columns=["symbol", "fundingRate", "fundingRateTimestamp", "dt"])
    fdf.to_csv(HIST / f"{sym}_funding.csv", index=False)
    fdf.to_parquet(HIST / f"{sym}_funding.parquet", index=False)
    print(f"  funding: {len(fdf)}", flush=True)


def buy_hold_pct(symbol: str) -> float:
    df = load_ohlc(symbol, "1h")
    if len(df) < 2:
        return float("nan")
    a = float(df.iloc[0]["close"])
    b = float(df.iloc[-1]["close"])
    return (b / a - 1.0) * 100.0 if a > 0 else float("nan")


def max_consec_losses(trades: list[Trade]) -> int:
    best = cur = 0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        if t.pnl <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def max_dd_from_trades(trades: list[Trade], start_cap: float = 160.0) -> tuple[float, float]:
    eq = peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        eq += t.pnl
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    dd = abs(max_dd)
    return dd, dd / start_cap * 100.0


def month_windows(trades: list[Trade], all_ts: list[int]) -> list[tuple[str, int, int]]:
    if not all_ts:
        return []
    t0, t1 = all_ts[0], all_ts[-1]
    span = t1 - t0
    cut1 = t0 + span // 3
    cut2 = t0 + 2 * span // 3
    return [
        ("M1", t0, cut1),
        ("M2", cut1, cut2),
        ("M3", cut2, t1 + 1),
    ]


def fmt_block(title: str, lines: list[str]) -> str:
    return "\n".join([f"## {title}", ""] + lines + [""])


def row_metrics(trades: list[Trade], orders: int, fills: int) -> dict:
    s = summarize_trades(trades, orders, fills)
    s["fills"] = fills
    s["fill_rate"] = fills / orders if orders else 0.0
    return s


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    # ---- Spec ----
    spec = """# Спецификация +H1+H2+TP2.5R (как в коде)

## База (v1)
- Тренд 1h: EMA20_1h > EMA50_1h → long; < → short (HTF join без look-ahead по close_ts).
- BTC-фильтр ON: запрет long если (1h_close−1h_open) < −1.5·ATR_1h BTC; short зеркально.
- Confirm 15m: касание зоны EMA + бычья/медвежья свеча (close>open и close>EMA20 для long).
- Baseline entry: limit = mid(open, close) confirm; TTL=2 бара 15m.
- Maker fill (S0): **касание** `low <= limit` (long) / `high >= limit` (short) — не строгое пробитие.
- SL baseline: за локальный экстремум 5 бар ± 0.5·ATR15.
- Нога $40, max 2 позиции портфеля, 1 позиция на символ.
- Fees S0: maker 0.02% вход, taker 0.055% выход, slip SL 0.02%.

## H1a
- Требует согласование 4h: EMA20_4h > EMA50_4h для long / < для short (также asof по закрытию бара).

## H1b
- `er_4h` на 15m = |close[i]−close[i−16]| / Σ|Δclose| за 16 шагов (4h окно).
- Порог: **expanding median** `er_4h_exp_med` по монете (каузально, без фикса 0.25).
- Вход только если `er_4h >= er_4h_exp_med`. `Cfg.er4h_fixed = None`.

## H2
- Зона отката: EMA20_15…EMA50_15 (с band 0.15·ATR).
- Импульс: от 12-бар экстремума откат ≥ **1.0·ATR15**.
- SL: `max(расстояние до pullback-экстремума + 0.5·ATR15, 1.5·ATR15)` от entry (`h2_min_sl_atr=1.5`).

## Exit TP2.5R
- Чистый TP = **2.5·R**, без partial / без BE / без trail.
- При SL и TP в одном баре: сначала SL (консервативно).
"""
    lines.append(spec)

    # ---- Load core ----
    print("Building CORE features…", flush=True)
    feats = {}
    funds = {}
    for sym in CORE + [BTC]:
        feats[sym] = build_features(sym)
        funds[sym] = load_funding(sym)
        print(f"  {sym}: {len(feats[sym])}", flush=True)
    btc_df = feats[BTC]
    core_feats = {s: feats[s] for s in CORE}

    cfg0 = best_cfg(name="S0_H1+H2+TP2.5R")
    print("Run S0 core…", flush=True)
    res0 = run(cfg0, core_feats, funds, btc_df)
    trades0: list[Trade] = res0["trades"]
    g0 = res0["global_stats"]

    # ---- Decomposition ----
    lines.append("# Декомпозиция S0 +H1+H2+TP2.5R (SOL+HYPE+ZEC, ~90д)\n")

    lines.append("### По тикерам")
    lines.append(
        "| symbol | fills | fill_rate | WR | PF | Exp_R | Exp_usd | PnL | avg_sl_pct | avg_hold_bars |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sym in CORE:
        st = res0["stats"][sym]
        tr = [t for t in trades0 if t.symbol == sym]
        s = row_metrics(tr, st.orders, st.fills)
        lines.append(
            f"| {sym} | {st.fills} | {s['fill_rate']:.1%} | {s['WR']:.1%} | {s['PF']:.2f} | "
            f"{s['Exp_R']:.3f} | {s['Exp_usd']:.3f} | {s['PnL']:.2f} | {s['avg_sl_pct']:.3f} | {s['avg_hold_bars']:.1f} |"
        )
    s_all = row_metrics(trades0, g0.orders, g0.fills)
    lines.append(
        f"| ALL | {g0.fills} | {s_all['fill_rate']:.1%} | {s_all['WR']:.1%} | {s_all['PF']:.2f} | "
        f"{s_all['Exp_R']:.3f} | {s_all['Exp_usd']:.3f} | {s_all['PnL']:.2f} | "
        f"{s_all['avg_sl_pct']:.3f} | {s_all['avg_hold_bars']:.1f} |"
    )

    lines.append("\n### Long vs Short + Buy&Hold фон")
    lines.append("| side | fills | WR | PF | Exp_R | PnL |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for side in ("long", "short"):
        tr = [t for t in trades0 if t.side == side]
        s = row_metrics(tr, len(tr), len(tr))
        lines.append(
            f"| {side} | {len(tr)} | {s['WR']:.1%} | {s['PF']:.2f} | {s['Exp_R']:.3f} | {s['PnL']:.2f} |"
        )
    lines.append("")
    lines.append("| asset | Buy&Hold 90д % |")
    lines.append("|---|---:|")
    for sym in BH:
        lines.append(f"| {sym} | {buy_hold_pct(sym):+.2f}% |")

    # Months
    all_ts = sorted(set().union(*[set(df["ts"].astype("int64")) for df in core_feats.values()]))
    lines.append("\n### Окна M1/M2/M3 (~30д)")
    lines.append("| window | start_utc | end_utc | fills | WR | PF | PnL | MaxDD |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for name, a, b in month_windows(trades0, all_ts):
        tr = [t for t in trades0 if a <= t.exit_ts < b]
        s = row_metrics(tr, len(tr), len(tr))
        dd, _ = max_dd_from_trades(tr)
        t0 = pd.to_datetime(a, unit="ms", utc=True)
        t1 = pd.to_datetime(b - 1, unit="ms", utc=True)
        lines.append(
            f"| {name} | {t0.date()} | {t1.date()} | {len(tr)} | {s['WR']:.1%} | {s['PF']:.2f} | "
            f"{s['PnL']:.2f} | {dd:.2f} |"
        )

    # Risk series
    mcl = max_consec_losses(trades0)
    dd_usd, dd_pct = max_dd_from_trades(trades0)
    lines.append("\n### Риск-серия")
    lines.append(f"- max consecutive losses: **{mcl}**")
    lines.append(f"- MaxDD: **${dd_usd:.2f}** ({dd_pct:.1f}% от $160)")
    lines.append(
        f"- баров с загрузкой 2/2 позиций: **{res0.get('max_pos_full_bars', 0)}** "
        f"(из {len(all_ts)} ts)"
    )

    # ---- Stress ----
    lines.append("\n# Стресс-тест исполнения\n")
    stress_cfgs = [
        best_cfg(name="S0_base", fill_through_pct=0.0, slip_exit=0.0002, entry_mode="maker"),
        best_cfg(name="S1_fill_through_0.02pct", fill_through_pct=0.0002, slip_exit=0.0002, entry_mode="maker"),
        best_cfg(name="S2_slip_SL_0.05pct", fill_through_pct=0.0, slip_exit=0.0005, entry_mode="maker"),
        best_cfg(
            name="S3_taker_entry",
            fill_through_pct=0.0,
            entry_mode="taker",
            slip_entry=0.0003,
            slip_exit=0.0003,
            maker_fee=0.0002,
            taker_fee=0.00055,
        ),
    ]
    lines.append("| scenario | fills | fill_rate | WR | PF | Exp_R | Exp_usd | PnL | MaxDD | fees+slip |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    stress_rows = []
    for cfg in stress_cfgs:
        print(f"Stress {cfg.name}…", flush=True)
        if cfg.name.startswith("S0"):
            res = res0
        else:
            res = run(cfg, core_feats, funds, btc_df)
        g = res["global_stats"]
        s = row_metrics(res["trades"], g.orders, g.fills)
        dd, _ = max_dd_from_trades(res["trades"])
        lines.append(
            f"| {cfg.name} | {g.fills} | {s['fill_rate']:.1%} | {s['WR']:.1%} | {s['PF']:.2f} | "
            f"{s['Exp_R']:.3f} | {s['Exp_usd']:.3f} | {s['PnL']:.2f} | {dd:.2f} | {s['fees_slip']:.2f} |"
        )
        stress_rows.append({"scenario": cfg.name, **s, "MaxDD": dd, "orders": g.orders, "fills_n": g.fills})

    # ---- OOS ----
    lines.append("\n# Out-of-Sample: AAVE / LINK / ENA\n")
    for sym in OOS_SYMBOLS:
        need = not (HIST / f"{sym}_15m.parquet").exists()
        if need:
            download_symbol(sym, 90)
        else:
            print(f"cache hit {sym}", flush=True)

    print("Building OOS features…", flush=True)
    oos_feats = {}
    oos_funds = {}
    for sym in OOS_SYMBOLS:
        oos_feats[sym] = build_features(sym)
        oos_funds[sym] = load_funding(sym)
        print(f"  {sym}: {len(oos_feats[sym])}", flush=True)

    # need BTC still
    cfg_oos = best_cfg(name="OOS_H1+H2+TP2.5R")
    lines.append("| universe | fills | WR | PF | Exp_R | Exp_usd | PnL | MaxDD |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    # together
    print("OOS basket…", flush=True)
    res_b = run(cfg_oos, oos_feats, oos_funds, btc_df)
    gb = res_b["global_stats"]
    sb = row_metrics(res_b["trades"], gb.orders, gb.fills)
    ddb, _ = max_dd_from_trades(res_b["trades"])
    lines.append(
        f"| AAVE+LINK+ENA | {gb.fills} | {sb['WR']:.1%} | {sb['PF']:.2f} | {sb['Exp_R']:.3f} | "
        f"{sb['Exp_usd']:.3f} | {sb['PnL']:.2f} | {ddb:.2f} |"
    )

    oos_rows = [{"universe": "AAVE+LINK+ENA", **sb, "MaxDD": ddb, "fills_n": gb.fills}]
    for sym in OOS_SYMBOLS:
        print(f"OOS {sym}…", flush=True)
        res = run(cfg_oos, {sym: oos_feats[sym]}, {sym: oos_funds[sym]}, btc_df)
        g = res["global_stats"]
        s = row_metrics(res["trades"], g.orders, g.fills)
        dd, _ = max_dd_from_trades(res["trades"])
        lines.append(
            f"| {sym} | {g.fills} | {s['WR']:.1%} | {s['PF']:.2f} | {s['Exp_R']:.3f} | "
            f"{s['Exp_usd']:.3f} | {s['PnL']:.2f} | {dd:.2f} |"
        )
        oos_rows.append({"universe": sym, **s, "MaxDD": dd, "fills_n": g.fills})

    # Verdict helper
    lines.append("\n# Вердикт (авто)\n")
    pf_oos = [r["PF"] for r in oos_rows if r["universe"] != "AAVE+LINK+ENA"]
    basket_pf = sb["PF"]
    month_pnls = []
    for name, a, b in month_windows(trades0, all_ts):
        tr = [t for t in trades0 if a <= t.exit_ts < b]
        month_pnls.append(sum(t.pnl for t in tr))
    long_tr = [t for t in trades0 if t.side == "long"]
    long_s = row_metrics(long_tr, len(long_tr), len(long_tr))
    s1 = next(r for r in stress_rows if r["scenario"].startswith("S1"))
    s3 = next(r for r in stress_rows if r["scenario"].startswith("S3"))

    lines.append(
        f"- Месяцы PnL: M1={month_pnls[0]:.2f}, M2={month_pnls[1]:.2f}, M3={month_pnls[2]:.2f} "
        f"(прибыль не в одном окне: {sum(1 for x in month_pnls if x > 0)}/3 > 0)."
    )
    lines.append(
        f"- Long PF={long_s['PF']:.2f}, PnL={long_s['PnL']:.2f} "
        f"(критичный провал long: {'да' if long_s['PF'] < 0.7 and long_s['PnL'] < -15 else 'нет'})."
    )
    lines.append(
        f"- Stress S1 PF={s1['PF']:.2f}, S3 PF={s3['PF']:.2f}."
    )
    oos_pf_txt = ", ".join(
        f"{r['universe']}={r['PF']:.2f}"
        for r in oos_rows
        if r["universe"] != "AAVE+LINK+ENA"
    )
    lines.append(f"- OOS per-coin PF: {oos_pf_txt}; basket PF={basket_pf:.2f}.")
    pass_oos = all(
        r["PF"] >= 1.05 for r in oos_rows if r["universe"] != "AAVE+LINK+ENA"
    ) or basket_pf >= 1.05
    verdict = "PASS (см. детали)" if pass_oos else "FAIL / слабо"
    lines.append(f"- Критерий эксперта PF>1.05–1.10 на нетронутых: {verdict}.")

    text = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "REPORT.md").write_text(text, encoding="utf-8")
    pd.DataFrame(stress_rows).to_csv(OUT / "stress.csv", index=False)
    pd.DataFrame(oos_rows).to_csv(OUT / "oos.csv", index=False)
    print("\n" + text)
    print(f"→ {OUT / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
