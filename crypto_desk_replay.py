#!/usr/bin/env python3
"""Прогон крипто-стола за сутки: journal + свечи Bybit, старый vs новый desk_policy.

    .venv/bin/python crypto_desk_replay.py
    .venv/bin/python crypto_desk_replay.py --hours 24
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from skills.crypto import desk_policy as policy
from skills.crypto.desk_policy import (
    PROBE_EMA_SLOW,
    PROBE_SHELF_BARS,
    PROBE_SHELF_DRIFT_BARS,
    PROBE_SHELF_DRIFT_PCT,
    PROBE_VOL_MULT,
    _median,
    cfg,
    ma_trend,
    pick_alts,
    probe_arm_action,
    probe_exit_reason,
    score_alloc,
)

API = "https://api.bybit.com/v5/market/kline"
FEE_SIDE = 0.0018
_MSK = timezone(timedelta(hours=5))


def _old_probe_shelf_setup(bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Щуп до коммита 1be6362: только суточная полка, без локального окна 2ч."""
    if len(bars) < PROBE_EMA_SLOW + 2:
        return None
    forming = bars[-1]
    closed = bars[:-1]
    if len(closed) < 21:
        return None
    shelf = closed[:-1][-PROBE_SHELF_BARS:]
    if len(shelf) < 20:
        return None
    try:
        high = max(float(bar["high"]) for bar in shelf)
        low = min(float(bar["low"]) for bar in shelf)
        vols = [float(bar["volume"]) for bar in shelf if float(bar["volume"]) > 0]
        price = float(forming["close"])
        impulse = max(float(forming.get("volume") or 0), float(closed[-1].get("volume") or 0))
    except (TypeError, ValueError, KeyError):
        return None
    if low <= 0 or high <= 0 or price <= 0 or not vols:
        return None
    limits = cfg()
    if (high - low) / low * 100.0 > limits.probe_shelf_range_pct:
        return None
    floor = high * (1.0 + limits.probe_break_min_pct / 100.0)
    ceiling = high * (1.0 + limits.probe_break_max_pct / 100.0)
    if price < floor or price > ceiling:
        return None
    typical = _median(vols)
    if typical <= 0 or impulse < typical * PROBE_VOL_MULT:
        return None
    prev_high = float(closed[-1].get("high") or 0)
    if prev_high >= high * (1.0 + limits.probe_break_min_pct / 100.0):
        return None
    tail = shelf[-PROBE_SHELF_DRIFT_BARS:]
    if len(tail) >= 2:
        old = float(tail[0].get("close") or 0)
        new = float(tail[-1].get("close") or 0)
        if old > 0 and abs(new / old - 1.0) * 100.0 > PROBE_SHELF_DRIFT_PCT:
            return None
    quiet = [float(bar.get("volume") or 0) for bar in shelf[:-2] if float(bar.get("volume") or 0) > 0]
    base = _median(quiet) if quiet else typical
    if base > 0:
        for bar in shelf[-2:]:
            if float(bar.get("volume") or 0) >= base * PROBE_VOL_MULT:
                return None
    return {"shelf": high, "price": price}


def _old_pick_alts(
    rows: list[dict[str, Any]],
    *,
    sleeve_pct: float | None = None,
    max_names: int | None = None,
    excluded: set[str] | None = None,
) -> dict[str, float]:
    limits = cfg()
    sleeve_pct = limits.sleeve_max_pct if sleeve_pct is None else float(sleeve_pct)
    max_names = limits.sleeve_max_names if max_names is None else int(max_names)
    slot = policy.alt_slot_pct(sleeve_pct, max_names)
    if slot <= 0:
        return {}
    blocked = {str(t).upper() for t in (excluded or set())}
    alts = [r for r in rows if str(r.get("ticker") or "").upper() in policy.sleeve_names()]
    held = sorted(
        (r for r in alts if policy._held_value(r) >= policy.ALT_HELD_MIN_USD),
        key=policy._held_value,
        reverse=True,
    )
    held_t = {str(r["ticker"]).upper() for r in held}
    fresh = sorted(
        (
            r
            for r in alts
            if str(r["ticker"]).upper() not in held_t
            and str(r["ticker"]).upper() not in blocked
            and policy.alt_entry_ok(r.get("bb_z"))
        ),
        key=lambda r: float(r["bb_z"]),
    )
    picked = (held + fresh)[: max(0, int(max_names))]
    return {str(r["ticker"]).upper(): slot for r in picked}


def fetch_klines(ticker: str, interval: str, *, end_ms: int | None, limit: int = 200) -> list[dict[str, float]]:
    params: dict[str, str] = {
        "category": "spot",
        "symbol": f"{ticker.upper()}USDT",
        "interval": interval,
        "limit": str(min(limit, 1000)),
    }
    if end_ms is not None:
        params["end"] = str(end_ms)
    resp = requests.get(API, params=params, timeout=20)
    resp.raise_for_status()
    raw = (resp.json().get("result") or {}).get("list") or []
    bars: list[dict[str, float]] = []
    for row in reversed(raw):
        if len(row) < 6:
            continue
        bars.append(
            {
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    return bars


def journal_lines(hours: float) -> list[str]:
    since = f"{int(hours)} hours ago"
    proc = subprocess.run(
        ["journalctl", "--user", "-u", "voice-assistant.service", "--since", since, "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["journalctl", "--user", "--since", since, "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
    return [line for line in proc.stdout.splitlines() if "[Крипта]" in line]


def parse_journal_events(lines: list[str]) -> dict[str, Any]:
    desk_re = re.compile(
        r"целевой портфель: (.+?) \(Скор-стол: (.+?)\)\s*$"
    )
    trade_re = re.compile(
        r"Дневник крипты: (\d{2}\.\d{2} \d{2}:\d{2}) (купил|продал) (\w+)"
    )
    desks: list[dict[str, str]] = []
    trades: list[dict[str, str]] = []
    probes: list[str] = []
    pocket: list[str] = []
    for line in lines:
        m = desk_re.search(line)
        if m:
            ts = line.split("python[")[0].strip() if "python[" in line else ""
            desks.append({"ts": ts, "brief": m.group(1), "why": m.group(2)})
        m = trade_re.search(line)
        if m:
            trades.append({"when": m.group(1), "side": m.group(2), "ticker": m.group(3)})
        if "щуп" in line.lower():
            probes.append(line.split("[INFO] ", 1)[-1].strip())
        if "карман AVA" in line:
            pocket.append(line.split("[INFO] ", 1)[-1].strip())
    return {"desks": desks, "trades": trades, "probes": probes, "pocket_ava": pocket}


def load_trades(path: str, since_epoch: float) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for row in raw.get("trades") or []:
        ts = float(row.get("ts_epoch") or 0)
        if ts >= since_epoch:
            out.append(row)
    return out


def _trend15_1h(bars15: list[dict[str, float]], i: int, bars60: list[dict[str, float]]) -> tuple[str, str]:
    c15 = [b["close"] for b in bars15[: i + 1]]
    ts = bars15[i]["ts"]
    c60 = [b["close"] for b in bars60 if b["ts"] <= ts]
    return ma_trend(c15), ma_trend(c60[-80:])


def simulate_probe_ticker(
    ticker: str,
    *,
    start_ms: int,
    end_ms: int,
    setup_fn,
    label: str,
) -> list[dict[str, Any]]:
    """Первый вход щупа на тикере в окне; один цикл buy→exit."""
    bars = fetch_klines(ticker, "15", end_ms=end_ms, limit=200)
    bars = [b for b in bars if b["ts"] >= start_ms]
    if len(bars) < PROBE_EMA_SLOW + 25:
        return []
    hour = fetch_klines(ticker, "60", end_ms=end_ms, limit=100)
    events: list[dict[str, Any]] = []
    armed: dict[str, float] | None = None
    armed_ts = 0.0
    in_pos = False
    entry = arm_px = peak = entry_low = 0.0
    shelf_level = 0.0
    for i in range(PROBE_EMA_SLOW + 2, len(bars)):
        window = bars[: i + 1]
        bar = bars[i]
        ts = bar["ts"] / 1000.0
        price = bar["close"]
        if not in_pos:
            setup = setup_fn(window)
            if setup and armed is None:
                armed = setup
                armed_ts = ts
                arm_px = float(setup["price"])
                shelf_level = float(setup["shelf"])
                events.append(
                    {
                        "kind": "arm",
                        "algo": label,
                        "ticker": ticker,
                        "ts": datetime.fromtimestamp(ts, _MSK).strftime("%d.%m %H:%M"),
                        "shelf": shelf_level,
                        "px": arm_px,
                    }
                )
            if armed is not None:
                t15, t1h = _trend15_1h(bars, i, hour)
                day_chg = None
                if i >= 96:
                    day_chg = (price / bars[i - 96]["close"] - 1.0) * 100.0
                act = probe_arm_action(
                    armed_ts=armed_ts,
                    now=ts,
                    price=price,
                    shelf=shelf_level,
                    trend15=t15,
                    day_chg=day_chg,
                    arm_px=arm_px,
                )
                if act == "drop":
                    armed = None
                elif act == "buy":
                    in_pos = True
                    entry = price * (1.0 + FEE_SIDE)
                    entry_low = price
                    peak = price
                    events.append(
                        {
                            "kind": "buy",
                            "algo": label,
                            "ticker": ticker,
                            "ts": datetime.fromtimestamp(ts, _MSK).strftime("%d.%m %H:%M"),
                            "px": price,
                        }
                    )
                    armed = None
        else:
            peak = max(peak, bar["high"])
            t15, _ = _trend15_1h(bars, i, hour)
            reason = probe_exit_reason(
                price=price,
                entry=entry,
                shelf=shelf_level,
                trend15=t15,
                arm_px=arm_px,
                peak=peak,
                entry_low=entry_low,
                qty=(policy.probe_quote() / entry) if entry > 0 else 0.0,
            )
            if reason:
                exit_px = price * (1.0 - FEE_SIDE)
                pnl = (exit_px / entry - 1.0) * policy.probe_quote()
                events.append(
                    {
                        "kind": "exit",
                        "algo": label,
                        "ticker": ticker,
                        "ts": datetime.fromtimestamp(ts, _MSK).strftime("%d.%m %H:%M"),
                        "px": price,
                        "reason": reason,
                        "pnl_usd": round(pnl, 2),
                    }
                )
                return events
    return events


def replay_sleeve_gram() -> str:
    """Сценарий суток: GRAM в рукаве до продажи, затем чёрный список."""
    policy.set_live_sleeve({"GRAM", "ARX"})
    rows_held = [
        {"ticker": "GRAM", "held_value": 85.0, "bb_z": -1.5},
        {"ticker": "SOL", "held_value": 80.0, "bb_z": -2.0},
        {"ticker": "NEAR", "held_value": 0, "bb_z": -3.2},
    ]
    excluded = {"GRAM", "SOL", "SUI", "NEAR"}
    old = _old_pick_alts(rows_held, excluded=excluded)
    new = pick_alts(rows_held, excluded=excluded, blacklist={"GRAM"})
    lines = [
        "Рукав (GRAM ещё на счёте, ~85$):",
        f"  старый pick_alts → {old}",
        f"  новый + blacklist GRAM → {new}",
    ]
    rows_after = [
        {"ticker": "SOL", "held_value": 80.0, "bb_z": -2.0},
        {"ticker": "NEAR", "held_value": 0, "bb_z": -3.2},
    ]
    old2 = _old_pick_alts(rows_after, excluded=excluded)
    new2 = pick_alts(rows_after, excluded=excluded, blacklist={"GRAM"})
    _a, why_new = score_alloc(rows_after, excluded=excluded, blacklist={"GRAM"})
    lines += [
        "После продажи GRAM (как 18:44 в journal):",
        f"  старый → {old2}",
        f"  новый → {new2}",
        f"  score_alloc: {why_new}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--trades", default="jarvis_crypto_trades.json")
    args = parser.parse_args()

    now = time.time()
    since = now - args.hours * 3600.0
    start_ms = int(since * 1000)
    end_ms = int(now * 1000)

    lines = journal_lines(args.hours)
    j = parse_journal_events(lines)
    live_trades = load_trades(args.trades, since)

    print(f"=== Journal за {args.hours:.0f} ч ({len(lines)} строк [Крипта]) ===\n")
    print(f"Полных столов: {len(j['desks'])}")
    for d in j["desks"]:
        print(f"  {d['ts']}: {d['brief']}")
    print(f"\nСделки в journal: {len(j['trades'])}")
    for t in j["trades"]:
        print(f"  {t['when']} {t['side']} {t['ticker']}")
    print(f"\nЩуп (log): {len(j['probes'])}")
    for p in j["probes"]:
        print(f"  {p}")
    print(f"\nКарман AVA (уник. фраз): {len(set(j['pocket_ava']))}")

    print(f"\n=== jarvis_crypto_trades.json с {datetime.fromtimestamp(since, _MSK):%d.%m %H:%M} ===")
    pnl_live = 0.0
    for row in live_trades:
        q = float(row.get("quote") or 0)
        side = str(row.get("side") or "")
        sign = -1 if side == "buy" else 1
        pnl_live += sign * q
        print(
            f"  {row.get('ts')} {side} {row.get('ticker')} "
            f"${q:.2f} @ {row.get('price')}"
        )
    print(f"  оборот buy-sell (грубо): {pnl_live:+.2f} $ quote")

    print("\n=== Рукав / blacklist (модель) ===")
    print(replay_sleeve_gram())

    tickers = sorted({str(r.get("ticker") or "").upper() for r in live_trades} | {"ARX", "AVA", "GRAM"})
    print("\n=== Щуп ARX: старый vs новый алгоритм на 15m свечах ===")
    live_arx = [r for r in live_trades if r.get("ticker") == "ARX"]
    if live_arx:
        buy_ts = min(float(r["ts_epoch"]) for r in live_arx if r.get("side") == "buy")
        print(f"  Живая сделка: buy @ {datetime.fromtimestamp(buy_ts, _MSK):%d.%m %H:%M}")
    for fn, name in ((policy.probe_shelf_setup, "новый"), (_old_probe_shelf_setup, "старый")):
        ev = simulate_probe_ticker("ARX", start_ms=start_ms, end_ms=end_ms, setup_fn=fn, label=name)
        if not ev:
            print(f"  {name}: входа не было")
            continue
        summary = " → ".join(f"{e['kind']} {e.get('ts', '')} {e.get('reason', '')} {e.get('pnl_usd', '')}" for e in ev)
        print(f"  {name}: {summary}")

    print("\n=== Локальные полки (только новый): тикеры из сделок ===")
    policy.set_live_sleeve({"GRAM", "ARX", "PUMP", "LIT", "DOOD"})
    for ticker in tickers:
        if ticker in ("GRAM",):
            continue
        bars = fetch_klines(ticker, "15", end_ms=end_ms, limit=1000)
        bars = [b for b in bars if b["ts"] >= start_ms]
        local_hits = 0
        daily_hits = 0
        for i in range(PROBE_EMA_SLOW + 2, len(bars)):
            w = bars[: i + 1]
            if policy.probe_shelf_setup(w):
                local_hits += 1
            if _old_probe_shelf_setup(w):
                daily_hits += 1
        only_new = local_hits - daily_hits
        if local_hits or daily_hits:
            print(f"  {ticker}: сигналов старый={daily_hits} новый={local_hits} (+только новый={max(0, only_new)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
