#!/usr/bin/env python3
"""
Выгрузка истории для бэктеста: 15m / 1h / 4h + funding.
Join без look-ahead: старшие ТФ сдвигаются на закрытие предыдущего бара.

Запуск:
  .venv/bin/python research_linear_download.py --days 90
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from research_linear.bybit_public import fetch_funding_history, fetch_klines, fnum

OUT = Path("research_linear_data/history")
SYMBOLS = ["SOLUSDT", "HYPEUSDT", "ZECUSDT", "BTCUSDT"]
INTERVALS = {"15": "15m", "60": "1h", "240": "4h"}


def klines_to_df(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "turnover"]
        )
    df = pd.DataFrame(
        rows,
        columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
    )
    for c in ("open", "high", "low", "close", "volume", "turnover"):
        df[c] = df[c].map(fnum)
    df["ts"] = df["ts"].astype("int64")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def shift_htf_to_15m(base_15: pd.DataFrame, htf: pd.DataFrame, label: str) -> pd.DataFrame:
    """
    Без look-ahead: на 15m-баре t доступен только ПОСЛЕДНИЙ ПОЛНОСТЬЮ ЗАКРЫТЫЙ HTF-бар.
    Bybit kline ts = open time. Бар [ts, ts+period) закрывается в ts+period.
    Значит на момент open 15m-бара t известны HTF с close_time = ts_htf+period <= t.
    Эквивалент: merge_asof backward по close_time_htf, затем данные HTF относятся к прошлым.
    """
    if htf.empty:
        out = base_15[["ts", "dt"]].copy()
        for col in ("open", "high", "low", "close", "volume", "turnover"):
            out[f"{label}_{col}"] = pd.NA
        return out

    period_ms = int(htf["ts"].diff().median())
    if period_ms <= 0 or pd.isna(period_ms):
        # fallback by label
        period_ms = {"1h": 3_600_000, "4h": 14_400_000}.get(label, 3_600_000)

    h = htf.copy()
    h["close_ts"] = h["ts"] + period_ms
    # Данные бара становятся известны только на close_ts → для asof к 15m open ts
    # используем close_ts как ключ; при равенстве 15m.open == htf.close это ок
    # (закрытие HTF совпало с open следующего 15m — уже известно).
    right = h.rename(
        columns={
            "open": f"{label}_open",
            "high": f"{label}_high",
            "low": f"{label}_low",
            "close": f"{label}_close",
            "volume": f"{label}_volume",
            "turnover": f"{label}_turnover",
            "ts": f"{label}_open_ts",
        }
    )
    left = base_15[["ts", "dt"]].copy()
    merged = pd.merge_asof(
        left.sort_values("ts"),
        right.sort_values("close_ts"),
        left_on="ts",
        right_on="close_ts",
        direction="backward",
    )
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    meta_rows = []
    for sym in args.symbols:
        print(f"=== {sym} ===", flush=True)
        frames = {}
        for iv, name in INTERVALS.items():
            print(f"  kline {name}…", flush=True)
            rows = fetch_klines(sym, iv, args.days)
            df = klines_to_df(rows)
            frames[name] = df
            raw_csv = OUT / f"{sym}_{name}.csv"
            raw_pq = OUT / f"{sym}_{name}.parquet"
            df.to_csv(raw_csv, index=False)
            df.to_parquet(raw_pq, index=False)
            span_d = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 86_400_000 if len(df) else 0
            print(f"    bars={len(df)} span≈{span_d:.1f}d → {raw_csv.name}", flush=True)
            meta_rows.append(
                {
                    "symbol": sym,
                    "tf": name,
                    "bars": len(df),
                    "span_days": round(float(span_d), 2),
                    "start": str(df["dt"].iloc[0]) if len(df) else "",
                    "end": str(df["dt"].iloc[-1]) if len(df) else "",
                }
            )
            time.sleep(0.05)

        print("  funding…", flush=True)
        fund = fetch_funding_history(sym, args.days)
        fdf = pd.DataFrame(fund)
        if not fdf.empty:
            fdf["fundingRate"] = fdf["fundingRate"].map(fnum)
            fdf["fundingRateTimestamp"] = fdf["fundingRateTimestamp"].astype("int64")
            fdf["dt"] = pd.to_datetime(fdf["fundingRateTimestamp"], unit="ms", utc=True)
            fdf = fdf.sort_values("fundingRateTimestamp").drop_duplicates("fundingRateTimestamp")
        else:
            fdf = pd.DataFrame(columns=["symbol", "fundingRate", "fundingRateTimestamp", "dt"])
        fdf.to_csv(OUT / f"{sym}_funding.csv", index=False)
        fdf.to_parquet(OUT / f"{sym}_funding.parquet", index=False)
        print(f"    funding rows={len(fdf)}", flush=True)

        # joined 15m dataset without look-ahead
        base = frames["15m"]
        if base.empty:
            print("  skip join: empty 15m", flush=True)
            continue
        joined = base.copy()
        for name in ("1h", "4h"):
            part = shift_htf_to_15m(base, frames[name], name)
            cols = [c for c in part.columns if c.startswith(f"{name}_") or c == "close_ts"]
            # drop helper close_ts duplicates
            keep = [c for c in part.columns if c not in {"ts", "dt"} or c.startswith(name)]
            # merge on ts
            add = part[["ts"] + [c for c in part.columns if c.startswith(f"{name}_")]].copy()
            joined = joined.merge(add, on="ts", how="left")

        # attach last known funding rate as-of (funding paid at timestamp — known after)
        if not fdf.empty:
            fund_asof = fdf.rename(
                columns={
                    "fundingRate": "funding_rate",
                    "fundingRateTimestamp": "funding_ts",
                }
            )[["funding_ts", "funding_rate"]]
            # for feature use: last funding ALREADY occurred (backward asof on bar open)
            joined = pd.merge_asof(
                joined.sort_values("ts"),
                fund_asof.sort_values("funding_ts"),
                left_on="ts",
                right_on="funding_ts",
                direction="backward",
            )
            # event stream for PnL: exact funding timestamps kept in separate file
        joined_path = OUT / f"{sym}_joined_15m.parquet"
        joined.to_parquet(joined_path, index=False)
        joined.to_csv(OUT / f"{sym}_joined_15m.csv", index=False)
        print(f"  joined → {joined_path.name} cols={list(joined.columns)[:12]}…", flush=True)
        time.sleep(0.05)

    meta = pd.DataFrame(meta_rows)
    meta.to_csv(OUT / "download_meta.csv", index=False)
    print("\nMeta:")
    print(meta.to_string(index=False))
    print(f"\nAll under {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
