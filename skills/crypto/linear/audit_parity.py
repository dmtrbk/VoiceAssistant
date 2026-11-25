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


def _audit_one(
    *,
    hours: int,
    symbols: list[str],
    journal_path: Path,
    frames: dict,
    btc_df,
    params: StrategyParams,
    since_ms: int,
    label: str,
) -> dict:
    core_sigs = []
    for sym in symbols:
        df = frames[sym]
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

    paper = [
        {
            "symbol": r["symbol"],
            "side": r["side"],
            "bar_ts": int(r["bar_ts"]),
            "limit_px": round(float(r["limit_px"]), 8),
            "sl": round(float(r["sl"]), 8),
            "tp": round(float(r["tp"]), 8),
        }
        for r in _load_jsonl(journal_path)
        if int(r.get("bar_ts") or 0) >= since_ms and str(r.get("symbol") or "").upper() in set(symbols)
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

    # Строгий parity = полное равенство множеств.
    # paper_consistent = все paper-сигналы есть в core без mismatch (слоты/kill режут only_core — ок).
    paper_consistent = not only_paper and not mismatches
    return {
        "label": label,
        "symbols": symbols,
        "core_signals": len(core_sigs),
        "paper_signals": len(paper),
        "matched": len(both) - len({m[0] for m in mismatches}),
        "only_core": [list(x) for x in only_core],
        "only_paper": [list(x) for x in only_paper],
        "mismatches": [
            {"key": list(k), "field": f, "core": c, "paper": p} for k, f, c, p in mismatches
        ],
        "parity_ok": not only_core and not only_paper and not mismatches,
        "paper_consistent": paper_consistent,
        "paper_keys": [list(k) for k in sorted(paper_map)],
        "core_keys": [list(k) for k in sorted(core_map)],
    }


def run_audit(
    *,
    hours: int = 48,
    shadow: bool = False,
    journal_dir: str | None = None,
    both: bool = False,
) -> int:
    cfg = default_config(paper=True)
    rest = BybitRest(testnet=cfg.testnet)
    params = StrategyParams(er4h_median_mode=cfg.er4h_median_mode, tp_r=cfg.tp_r)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = now_ms - hours * 3600_000
    jdir = Path(journal_dir) if journal_dir else cfg.runtime_dir

    want = set(cfg.symbols) | {cfg.btc_symbol} | set(cfg.shadow_symbols)
    raw_frames = {}
    for sym in sorted(want):
        raw_frames[sym] = {
            "15m": fetch_klines_paginated(rest, sym, "15", cfg.warm_bars_15m),
            "1h": fetch_klines_paginated(rest, sym, "60", cfg.warm_bars_1h),
            "4h": fetch_klines_paginated(rest, sym, "240", cfg.warm_bars_4h),
        }
    feats = {
        s: build_features(raw_frames[s]["15m"], raw_frames[s]["1h"], raw_frames[s]["4h"])
        for s in raw_frames
    }
    btc_df = feats[cfg.btc_symbol]

    reports = []
    if both or not shadow:
        reports.append(
            _audit_one(
                hours=hours,
                symbols=list(cfg.symbols),
                journal_path=jdir / "paper_signals.jsonl",
                frames=feats,
                btc_df=btc_df,
                params=params,
                since_ms=since_ms,
                label="core",
            )
        )
    if both or shadow:
        reports.append(
            _audit_one(
                hours=hours,
                symbols=list(cfg.shadow_symbols),
                journal_path=jdir / "shadow_aave_signals.jsonl",
                frames=feats,
                btc_df=btc_df,
                params=params,
                since_ms=since_ms,
                label="shadow",
            )
        )

    report = {
        "hours": hours,
        "since_ms": since_ms,
        "journal_dir": str(jdir),
        "reports": reports,
        "parity_ok": all(r["parity_ok"] for r in reports),
        "paper_consistent": all(r["paper_consistent"] for r in reports),
    }
    out = cfg.path("parity_audit.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"→ {out}")
    # Для smoke TZ: достаточно paper_consistent (journal ⊆ offline core).
    return 0 if report["paper_consistent"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--both", action="store_true", help="Core + Shadow в одном прогоне")
    ap.add_argument("--journal-dir", default=None, help="каталог с paper/shadow jsonl")
    args = ap.parse_args()
    return run_audit(
        hours=args.hours,
        shadow=args.shadow,
        journal_dir=args.journal_dir,
        both=args.both,
    )


if __name__ == "__main__":
    sys.exit(main())
