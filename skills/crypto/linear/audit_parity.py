#!/usr/bin/env python3
"""Ежедневная parity-сверка: REST свечи → strategy_core vs paper_signals.jsonl."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .bybit_rest import BybitRest
from .config import default_config
from .strategy_core import StrategyParams, build_features, signal_at_bar
from .warm import fetch_klines_paginated


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def run_audit(*, hours: int = 48, shadow: bool = False) -> int:
    cfg = default_config(paper=True)
    rest = BybitRest(testnet=cfg.testnet)
    params = StrategyParams(er4h_median_mode=cfg.er4h_median_mode, tp_r=cfg.tp_r)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = now_ms - hours * 3600_000

    symbols = list(cfg.shadow_symbols if shadow else cfg.symbols)
    btc = cfg.btc_symbol

    # warm enough history for indicators
    frames = {}
    for sym in set(symbols) | {btc}:
        frames[sym] = {
            "15m": fetch_klines_paginated(rest, sym, "15", cfg.warm_bars_15m),
            "1h": fetch_klines_paginated(rest, sym, "60", cfg.warm_bars_1h),
            "4h": fetch_klines_paginated(rest, sym, "240", cfg.warm_bars_4h),
        }
    feats = {s: build_features(frames[s]["15m"], frames[s]["1h"], frames[s]["4h"]) for s in frames}
    btc_df = feats[btc]

    core_sigs = []
    for sym in symbols:
        df = feats[sym]
        for i in range(len(df)):
            ts = int(df.iloc[i]["ts"])
            if ts < since_ms:
                continue
            sig = signal_at_bar(sym, df, i, btc_df, params)
            if sig:
                core_sigs.append(
                    {
                        "symbol": sig.symbol,
                        "side": sig.side,
                        "bar_ts": sig.bar_ts,
                        "limit_px": round(sig.limit_px, 8),
                        "sl": round(sig.sl, 8),
                        "tp": round(sig.tp, 8),
                    }
                )

    journal_name = "shadow_aave_signals.jsonl" if shadow else "paper_signals.jsonl"
    paper = [
        {
            "symbol": r["symbol"],
            "side": r["side"],
            "bar_ts": int(r["bar_ts"]),
            "limit_px": round(float(r["limit_px"]), 8),
            "sl": round(float(r["sl"]), 8),
            "tp": round(float(r["tp"]), 8),
        }
        for r in _load_jsonl(cfg.path(journal_name))
        if int(r.get("bar_ts") or 0) >= since_ms
    ]

    def key(r: dict) -> tuple:
        return (r["symbol"], r["side"], r["bar_ts"])

    core_map = {key(r): r for r in core_sigs}
    paper_map = {key(r): r for r in paper}
    only_core = sorted(set(core_map) - set(paper_map))
    only_paper = sorted(set(paper_map) - set(core_map))
    both = sorted(set(core_map) & set(paper_map))
    mismatches = []
    for k in both:
        a, b = core_map[k], paper_map[k]
        for field in ("limit_px", "sl", "tp"):
            if abs(float(a[field]) - float(b[field])) > 1e-6 * max(1.0, abs(float(a[field]))):
                mismatches.append((k, field, a[field], b[field]))

    report = {
        "hours": hours,
        "shadow": shadow,
        "core_signals": len(core_sigs),
        "paper_signals": len(paper),
        "matched": len(both) - len({m[0] for m in mismatches}),
        "only_core": [list(x) for x in only_core],
        "only_paper": [list(x) for x in only_paper],
        "mismatches": [
            {"key": list(k), "field": f, "core": c, "paper": p} for k, f, c, p in mismatches
        ],
        "parity_ok": not only_core and not only_paper and not mismatches,
    }
    out = cfg.path("parity_audit.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"→ {out}")
    return 0 if report["parity_ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--shadow", action="store_true")
    args = ap.parse_args()
    return run_audit(hours=args.hours, shadow=args.shadow)


if __name__ == "__main__":
    sys.exit(main())
