#!/usr/bin/env python3
"""
Crypto-only скринер Bybit linear USDT Perpetual.
Фильтры: min_usd <= 10, spread <= 0.025%. Топ-15 по обороту + weekly ZEC/HYPE.

Запуск:
  .venv/bin/python research_linear_screener.py --days 45
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

from research_linear.bybit_public import (
    fetch_instruments_linear,
    fetch_klines,
    fetch_tickers_linear,
    fnum,
    orderbook_spread_depth,
)
from research_linear.metrics import (
    atr_pct_median,
    atr_pct_series,
    closes,
    corr,
    efficiency_ratio,
    hourly_returns,
    wick_share_mean,
)

OUT_DIR = Path("research_linear_data")


def fmt(x: float, d: int = 4) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{d}f}"


def weekly_stability(symbol: str, days: int = 90) -> list[dict]:
    """Понедельные turnover proxy (sum quote volume) и median ATR15%."""
    rows = fetch_klines(symbol, "15", days)
    if not rows:
        return []
    # week bucket by UTC ms
    week_ms = 7 * 24 * 60 * 60 * 1000
    buckets: dict[int, list] = {}
    for r in rows:
        ts = int(r[0])
        key = ts // week_ms
        buckets.setdefault(key, []).append(r)
    out = []
    for key in sorted(buckets):
        chunk = buckets[key]
        # Bybit kline: [start, open, high, low, close, volume, turnover]
        turnover = sum(fnum(r[6]) for r in chunk)
        atr = atr_pct_median(chunk)
        atr_s = atr_pct_series(chunk)
        out.append(
            {
                "week_start_ms": key * week_ms,
                "bars": len(chunk),
                "turnover_usd": turnover,
                "atr15_median_pct": atr,
                "atr15_p25": float(sorted(atr_s)[len(atr_s) // 4]) if atr_s else float("nan"),
                "atr15_p75": float(sorted(atr_s)[3 * len(atr_s) // 4]) if atr_s else float("nan"),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="окно метрик ATR/ER/wick/corr (дней)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--max-min-usd", type=float, default=10.0)
    ap.add_argument("--max-spread", type=float, default=0.025)
    ap.add_argument("--min-turnover", type=float, default=20e6)
    ap.add_argument("--weekly-days", type=int, default=90)
    args = ap.parse_args()
    days = max(30, min(60, args.days))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"=== Crypto screener linear | metrics window={days}d ===", flush=True)
    print(
        "Период метрик ATR/ER/wick/corr: "
        f"{days} календарных дней; "
        f"15m ≈ {days * 96} баров, 1h ≈ {days * 24} баров "
        "(фактически — сколько вернул API).",
        flush=True,
    )
    print("ER_15m = ER на окне 1 бар 15m (=1); используем ER на 1×15m path vs net за 1 бар — "
          "практично: er_15m = ER(closes15, bars=1) бессмысленен; "
          "здесь er_15m := ER на 4 бара 15m (1h path), er_1h := ER на 4×1h (4h), "
          "er_4h := ER на 16×15m (4h).", flush=True)

    instruments = fetch_instruments_linear()
    tickers = fetch_tickers_linear()
    print(f"instruments crypto-ish: {len(instruments)}", flush=True)

    ranked: list[tuple[float, str]] = []
    for sym, inst in instruments.items():
        t = tickers.get(sym)
        if not t:
            continue
        turnover = fnum(t.get("turnover24h"))
        if turnover < args.min_turnover:
            continue
        ranked.append((turnover, sym))
    ranked.sort(reverse=True)

    # Need BTC + SOL for corr even if filtered out
    need_extra = ["BTCUSDT", "SOLUSDT"]
    analyze: list[str] = []
    for _, sym in ranked:
        if sym not in analyze:
            analyze.append(sym)
        if len(analyze) >= max(args.top * 3, 40):
            break
    for sym in need_extra:
        if sym in instruments and sym not in analyze:
            analyze.append(sym)

    rows_out: list[dict] = []
    rets: dict[str, list[float]] = {}
    k15_cache: dict[str, list] = {}
    k1h_cache: dict[str, list] = {}

    for i, sym in enumerate(analyze, 1):
        inst = instruments[sym]
        t = tickers.get(sym) or {}
        last = fnum(t.get("lastPrice"))
        min_usd = inst["min_qty"] * last if last > 0 else float("nan")
        print(f"[{i}/{len(analyze)}] {sym} …", flush=True)
        try:
            spread, depth01 = orderbook_spread_depth(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  orderbook fail: {exc}", flush=True)
            continue
        k15 = fetch_klines(sym, "15", days)
        k1h = fetch_klines(sym, "60", days)
        k15_cache[sym] = k15
        k1h_cache[sym] = k1h
        c15 = closes(k15)
        c1h = closes(k1h)
        # er_15m: Efficiency Ratio на окне 4×15m (=1h) — «качество хода» короткого ТФ
        # er_1h: ER на окне 4×1h (=4h)
        # er_4h: ER на окне 16×15m (=4h) — дублирует горизонт, но на 15m-барах
        rec = {
            "symbol": sym,
            "min_usd": min_usd,
            "turnover_24h": fnum(t.get("turnover24h")),
            "spread_pct": spread,
            "depth_01pct_usd": depth01,
            "funding_rate": fnum(t.get("fundingRate")),
            "funding_interval_h": inst["funding_interval_h"],
            "atr15_pct": atr_pct_median(k15),
            "atr1h_pct": atr_pct_median(k1h),
            "er_15m": efficiency_ratio(c15, 4),
            "er_1h": efficiency_ratio(c1h, 4),
            "er_4h": efficiency_ratio(c15, 16),
            "wick_share_15m": wick_share_mean(k15),
            "bars_15m": len(k15),
            "bars_1h": len(k1h),
            "metrics_days": days,
            "corr_btc": float("nan"),
            "corr_sol": float("nan"),
            "pass_filter": (
                (not math.isnan(min_usd) and min_usd <= args.max_min_usd)
                and (not math.isnan(spread) and spread <= args.max_spread)
            ),
        }
        rets[sym] = hourly_returns(k1h)
        rows_out.append(rec)
        time.sleep(0.05)

    btc_r = rets.get("BTCUSDT", [])
    sol_r = rets.get("SOLUSDT", [])
    for rec in rows_out:
        r = rets.get(rec["symbol"], [])
        rec["corr_btc"] = corr(r, btc_r) if rec["symbol"] != "BTCUSDT" else 1.0
        rec["corr_sol"] = corr(r, sol_r) if rec["symbol"] != "SOLUSDT" else 1.0

    passed = [r for r in rows_out if r["pass_filter"]]
    passed.sort(key=lambda x: -x["turnover_24h"])
    top = passed[: args.top]

    cols = [
        "symbol",
        "min_usd",
        "turnover_24h",
        "spread_pct",
        "depth_01pct_usd",
        "funding_rate",
        "funding_interval_h",
        "atr15_pct",
        "atr1h_pct",
        "er_15m",
        "er_1h",
        "er_4h",
        "wick_share_15m",
        "corr_btc",
        "corr_sol",
        "bars_15m",
        "bars_1h",
        "metrics_days",
    ]

    csv_path = OUT_DIR / "screener_crypto_top15.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in top:
            w.writerow({k: r.get(k) for k in cols})

    md_path = OUT_DIR / "screener_crypto_top15.md"
    lines = [
        f"# Crypto USDT Perp screener (top {len(top)})",
        "",
        f"- Метрики ATR / ER / wick / corr: **{days} дней** "
        f"(15m bars≈{days*96}, 1h bars≈{days*24}; факт в столбцах bars_*).",
        "- Фильтры: `min_usd <= $10`, `spread_pct <= 0.025%`, crypto-only (без акций/сырья/индексов).",
        "- `er_15m` = ER на 4×15m (1h-окно); `er_1h` = ER на 4×1h; `er_4h` = ER на 16×15m.",
        "- `wick_share_15m` = mean((H-L-|C-O|)/(H-L)) на 15m.",
        "- `corr_*` — корреляция почасовых доходностей за то же окно.",
        "",
        "| " + " | ".join(cols[:15]) + " |",
        "| " + " | ".join(["---"] * 15) + " |",
    ]
    for r in top:
        cells = []
        for k in cols[:15]:
            v = r[k]
            if isinstance(v, float):
                if k.startswith("turnover"):
                    cells.append(f"{v/1e6:.1f}M")
                elif k.startswith("depth"):
                    cells.append(f"{v/1e3:.1f}k")
                elif k == "funding_rate":
                    cells.append(f"{v*100:.4f}%")
                else:
                    cells.append(fmt(v, 4))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "\n".join(lines[:8]))
    print(f"\nCSV → {csv_path}")
    print(f"MD  → {md_path}")
    if len(top) < args.top:
        print(f"WARNING: только {len(top)} контрактов прошли фильтр (просили {args.top})")

    # Weekly ZEC / HYPE
    for sym in ("ZECUSDT", "HYPEUSDT"):
        print(f"\n=== Weekly stability {sym} ({args.weekly_days}d) ===", flush=True)
        weeks = weekly_stability(sym, args.weekly_days)
        wcsv = OUT_DIR / f"weekly_{sym}.csv"
        with wcsv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=[
                    "week_start_ms",
                    "bars",
                    "turnover_usd",
                    "atr15_median_pct",
                    "atr15_p25",
                    "atr15_p75",
                ],
            )
            w.writeheader()
            for row in weeks:
                w.writerow(row)
                ts = row["week_start_ms"] / 1000
                import datetime as dt

                label = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                print(
                    f"  week {label}: bars={row['bars']:4d}  "
                    f"turn=${row['turnover_usd']/1e6:7.2f}M  "
                    f"ATR15 med={fmt(row['atr15_median_pct'],3)}%  "
                    f"p25={fmt(row['atr15_p25'],3)} p75={fmt(row['atr15_p75'],3)}"
                )
        print(f"  → {wcsv}")
        if weeks:
            turns = [w["turnover_usd"] for w in weeks if w["turnover_usd"] > 0]
            atrs = [w["atr15_median_pct"] for w in weeks if not math.isnan(w["atr15_median_pct"])]
            if turns:
                tmin, tmax = min(turns), max(turns)
                print(
                    f"  turnover range: ${tmin/1e6:.1f}M … ${tmax/1e6:.1f}M "
                    f"(max/min={tmax/tmin:.2f}x)"
                )
            if atrs:
                amin, amax = min(atrs), max(atrs)
                print(
                    f"  ATR15 med range: {amin:.3f}% … {amax:.3f}% "
                    f"(max/min={amax/amin:.2f}x)"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
