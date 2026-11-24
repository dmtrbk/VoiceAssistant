#!/usr/bin/env python3
"""Суточный probe-only replay по топ-N монетам по росту за 24ч (на старте окна).

    .venv/bin/python crypto_probe_top_gainers_sweep.py
    .venv/bin/python crypto_probe_top_gainers_sweep.py --end "2026-10-30 06:04" --top 15 --pool 80
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from skills.crypto import desk_policy as pol
from skills.crypto.desk_policy import (
    PROBE_MAX_LEGS,
    PROBE_SCAN_MANDATORY_TOP,
    probe_arm_action,
    probe_candle_buy_ok,
    probe_exit_reason,
    probe_pick_setup,
    probe_reserve_quote,
    probe_scan_ranked_rows,
    probe_trends_ok,
    cfg,
)

import crypto_desk_full_replay as replay

YEKT = timezone(timedelta(hours=5))
API = "https://api.bybit.com/v5/market"


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-probe-top-sweep/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def liquid_spot_rows(min_turnover: float) -> list[dict[str, Any]]:
    rows = (_get(f"{API}/tickers?category=spot").get("result") or {}).get("list") or []
    out: list[dict[str, Any]] = []
    for item in rows:
        sym = str(item.get("symbol") or "")
        if not sym.endswith("USDT") or pol._leveraged_base(sym[:-4]):
            continue
        base = sym[:-4]
        if not base or base in pol.SLEEVE_SKIP:
            continue
        try:
            turn = float(item.get("turnover24h") or 0)
            chg = float(item.get("price24hPcnt") or 0) * 100.0
            px = float(item.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if turn < min_turnover or px <= 0:
            continue
        out.append({"ticker": base, "turnover": turn, "chg_live": chg})
    out.sort(key=lambda r: r["chg_live"], reverse=True)
    return out


def top_gainers_at_ts(
    series: dict[str, dict[str, list]],
    tickers: list[str],
    ts: float,
    prices: dict[str, float],
    *,
    top_n: int,
    min_day_pct: float,
) -> list[tuple[float, str]]:
    ranked: list[tuple[float, str]] = []
    for ticker in tickers:
        if ticker not in series:
            continue
        px = prices.get(ticker) or 0
        day = replay.day_chg_at(series[ticker]["15"], ts, px)
        if day is None or day < min_day_pct:
            continue
        ranked.append((float(day), ticker))
    ranked.sort(reverse=True)
    return ranked[:top_n]


@dataclass
class Variant:
    name: str
    patch: Callable[[], None]


def _reset_policy() -> None:
    importlib.reload(pol)


def _variants() -> list[Variant]:
    def tier1() -> None:
        pol.read_probe_tune = lambda: {"tier": 1}  # type: ignore[method-assign]

    def tier3() -> None:
        pol.read_probe_tune = lambda: {"tier": 3}  # type: ignore[method-assign]

    def tier4() -> None:
        pol.read_probe_tune = lambda: {"tier": 4}  # type: ignore[method-assign]

    def day_min_m3() -> None:
        tier1()
        pol.PROBE_DAY_MIN_PCT = -3.0

    def room4() -> None:
        tier1()
        pol.PROBE_CANDLE_MIN_DAY_ROOM_PCT = 4.0

    def body3() -> None:
        tier1()
        pol.PROBE_CANDLE_MAX_BODY_PCT = 3.0

    def combo_m3_room4() -> None:
        tier1()
        pol.PROBE_DAY_MIN_PCT = -3.0
        pol.PROBE_CANDLE_MIN_DAY_ROOM_PCT = 4.0

    return [
        Variant("tier1_baseline", tier1),
        Variant("tier3_candle_impulse", tier3),
        Variant("tier4_loose_body", tier4),
        Variant("day_min_-3", day_min_m3),
        Variant("room_4pct", room4),
        Variant("max_body_3pct", body3),
        Variant("day-3_room4", combo_m3_room4),
    ]


def run_probe_on_universe(
    *,
    series: dict[str, dict[str, list]],
    scan_tickers: list[str],
    start_ts: float,
    end_ts: float,
    equity: float,
) -> dict[str, Any]:
    limits = cfg()
    probe_usd = float(limits.probe_usd)
    grid = [
        b["ts"]
        for b in series.get(scan_tickers[0], {}).get("5", [])
        if start_ts <= b["ts"] <= end_ts
    ]
    if not grid and scan_tickers:
        t0 = scan_tickers[0]
        grid = [b["ts"] for b in series[t0]["5"] if start_ts <= b["ts"] <= end_ts]

    book = replay.Book(cash=equity, start_equity=equity)
    blacklist: set[str] = set()

    def prices_now(ts: float) -> dict[str, float]:
        out: dict[str, float] = {}
        for ticker in scan_tickers:
            if ticker not in series:
                continue
            px = replay.px_at(series[ticker]["5"] or series[ticker]["15"], ts)
            if px > 0:
                out[ticker] = px
        return out

    for ts in grid:
        prices = prices_now(ts)
        for leg in list(book.open_kinds("probe")):
            bars15 = replay.window_upto(series[leg.ticker]["15"], ts, 130)
            if len(bars15) < 50:
                continue
            price = prices.get(leg.ticker) or 0
            if price <= 0:
                continue
            leg.peak = max(leg.peak, price)
            trend = pol.ma_trend([b["close"] for b in bars15])
            reason = probe_exit_reason(
                price=price,
                entry=leg.entry,
                shelf=leg.shelf,
                trend15=trend,
                arm_px=leg.arm_px,
                peak=leg.peak,
                entry_low=leg.low,
                algo=leg.algo,
                qty=leg.qty,
            )
            if reason:
                replay.sell(book, leg, ts=ts, price=price, reason=reason)

        still_arms: list[replay.Arm] = []
        for arm in book.probe_arms:
            if arm.ticker not in series:
                continue
            bars15 = replay.window_upto(series[arm.ticker]["15"], ts, 130)
            if len(bars15) < 50:
                still_arms.append(arm)
                continue
            price = prices.get(arm.ticker) or 0
            if price <= 0:
                still_arms.append(arm)
                continue
            trend = pol.ma_trend([b["close"] for b in bars15])
            day = replay.day_chg_at(series[arm.ticker]["15"], ts, price)
            week = replay.week_chg_at(series[arm.ticker]["60"], ts, price)
            action = probe_arm_action(
                armed_ts=arm.ts,
                now=ts,
                price=price,
                shelf=arm.shelf,
                trend15=trend,
                day_chg=day,
                arm_px=arm.px,
                week_chg=week,
                algo=arm.algo,
            )
            if action == "wait":
                still_arms.append(arm)
                continue
            if action == "drop":
                continue
            if book.cash < probe_usd * 0.5:
                still_arms.append(arm)
                continue
            if len(book.open_kinds("probe")) >= PROBE_MAX_LEGS:
                continue
            bars5 = replay.window_upto(series[arm.ticker]["5"], ts, 5)
            if arm.algo == pol.PROBE_ALGO_CANDLE and bars5:
                live5 = bars5[-1]
                if not probe_candle_buy_ok(
                    price,
                    float(live5.get("high") or 0),
                    float(live5.get("low") or 0),
                    float(live5.get("open") or 0),
                ):
                    continue
            low = float(bars5[-1]["low"]) if bars5 else price
            if replay.buy(
                book,
                ts=ts,
                kind="probe",
                ticker=arm.ticker,
                price=price,
                quote=min(probe_usd, book.cash * 0.998),
                algo=arm.algo,
                shelf=arm.shelf,
                arm_px=arm.px,
                low=low,
            ):
                continue
            still_arms.append(arm)
        book.probe_arms = still_arms

        if book.probe_count() >= PROBE_MAX_LEGS:
            continue
        blocked = {leg.ticker for leg in book.legs} | {a.ticker for a in book.probe_arms} | blacklist

        probe_rows: list[dict[str, Any]] = []
        for ticker in scan_tickers:
            if ticker in blocked or ticker not in series:
                continue
            price = prices.get(ticker) or 0
            day = replay.day_chg_at(series[ticker]["15"], ts, price) or 0.0
            bars15 = replay.window_upto(series[ticker]["15"], ts, 100)
            if len(bars15) < 10 or price <= 0:
                continue
            high = max(float(b["high"]) for b in bars15[-96:])
            low = min(float(b["low"]) for b in bars15[-96:])
            probe_rows.append(
                {
                    "ticker": ticker,
                    "chg": day,
                    "price": price,
                    "high": high,
                    "low": low,
                    "turnover": 2_000_000.0,
                }
            )
        order = pol.probe_scan_algo_order()
        for _rank, prow in probe_scan_ranked_rows(probe_rows):
            if book.probe_count() >= PROBE_MAX_LEGS:
                break
            ticker = str(prow["ticker"])
            bars15 = replay.window_upto(series[ticker]["15"], ts, 130)
            bars5 = replay.window_upto(series[ticker]["5"], ts, 90)
            if len(bars15) < 60 or len(bars5) < 20:
                continue
            price = prices.get(ticker) or 0
            day = replay.day_chg_at(series[ticker]["15"], ts, price)
            week = replay.week_chg_at(series[ticker]["60"], ts, price)
            setup = probe_pick_setup(
                replay.as_ohlc(bars15),
                replay.as_ohlc(bars5),
                order=order,
                day_chg=day,
                day_high=float(prow.get("high") or 0),
            )
            if not setup:
                continue
            algo = str(setup.get("algo") or "candle")
            trend = pol.ma_trend([b["close"] for b in bars15])
            bars60 = replay.window_upto(series[ticker]["60"], ts, 80)
            t1h = pol.ma_trend([b["close"] for b in bars60]) if len(bars60) >= 50 else "neutral"
            if not probe_trends_ok(trend, t1h, day, week_chg=week, algo=algo):
                continue
            book.probe_arms.append(
                replay.Arm(
                    kind="probe",
                    ticker=ticker,
                    ts=ts,
                    shelf=float(setup["shelf"]),
                    px=float(setup["price"]),
                    algo=algo,
                    hold_sec=float(setup.get("hold_sec") or 0),
                    chase_pct=float(setup.get("chase_pct") or 0),
                )
            )

    prices = prices_now(grid[-1]) if grid else {}
    finish = book.mtm(prices)
    trades = [e for e in book.events if e["kind"] == "probe" and e["side"] in ("buy", "sell")]
    return {
        "start": equity,
        "finish": round(finish, 2),
        "pnl": round(finish - equity, 2),
        "realized": round(book.realized, 2),
        "buys": sum(1 for e in trades if e["side"] == "buy"),
        "sells": sum(1 for e in trades if e["side"] == "sell"),
        "trades": [
            {
                "t": e["t"].strftime("%d.%m %H:%M"),
                "side": e["side"],
                "ticker": e["ticker"],
                "algo": e.get("algo") or "",
                "pnl": round(float(e.get("pnl") or 0), 2),
                "reason": e.get("reason") or "",
            }
            for e in trades
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--equity", type=float, default=404.0)
    parser.add_argument("--end", default="", help="YEKT YYYY-MM-DD HH:MM")
    parser.add_argument("--top", type=int, default=15, help="Топ по day pct на старте окна")
    parser.add_argument("--pool", type=int, default=80, help="Сколько ликвидных тикеров качать свечи")
    parser.add_argument("--min-turnover", type=float, default=250_000.0)
    parser.add_argument("--out", default="probe-top-gainers-sweep.md")
    args = parser.parse_args()

    if str(args.end or "").strip():
        end = datetime.strptime(str(args.end).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=YEKT)
    else:
        end = datetime.now(YEKT).replace(second=0, microsecond=0)
    start = end - timedelta(hours=args.hours)
    start_ts = start.timestamp()
    end_ts = end.timestamp()

    liquid = liquid_spot_rows(float(args.min_turnover))
    download_list = [r["ticker"] for r in liquid[: int(args.pool)]]
    print(
        f"Окно {start.strftime('%d.%m %H:%M')}–{end.strftime('%d.%m %H:%M')} YEKT | "
        f"pool {len(download_list)} | top {args.top} | mandatory scan top {PROBE_SCAN_MANDATORY_TOP}"
    )

    warm_ms = int((start - timedelta(days=10)).timestamp() * 1000)
    end_ms = int(end_ts * 1000)
    series: dict[str, dict[str, list]] = {}
    for i, ticker in enumerate(download_list, 1):
        sym = ticker + "USDT"
        try:
            series[ticker] = {
                "5": replay.klines(sym, "5", warm_ms, end_ms),
                "15": replay.klines(sym, "15", warm_ms, end_ms),
                "60": replay.klines(sym, "60", warm_ms, end_ms),
            }
        except Exception as exc:
            print(f"  skip {ticker}: {exc}")
        if i % 10 == 0:
            print(f"  klines …{i}/{len(download_list)}")
        time.sleep(0.02)

    px0: dict[str, float] = {}
    for ticker, bars in series.items():
        px = replay.px_at(bars["5"] or bars["15"], start_ts)
        if px > 0:
            px0[ticker] = px

    top_start = top_gainers_at_ts(
        series,
        list(series.keys()),
        start_ts,
        px0,
        top_n=int(args.top),
        min_day_pct=0.01,
    )
    scan_tickers = [t for _, t in top_start]
    print("Топ на старте окна (24h %):")
    for chg, t in top_start:
        print(f"  {t:8s} {chg:+.1f}%")

    results: list[dict[str, Any]] = []
    for var in _variants():
        _reset_policy()
        var.patch()
        print(f"  прогон {var.name}…")
        out = run_probe_on_universe(
            series=series,
            scan_tickers=scan_tickers,
            start_ts=start_ts,
            end_ts=end_ts,
            equity=float(args.equity),
        )
        out["variant"] = var.name
        results.append(out)
        print(f"    closed ${out['realized']:+.2f} | {out['buys']} buys | finish ${out['finish']:.2f}")

    results.sort(key=lambda r: r["realized"], reverse=True)

    lines = [
        "# Probe sweep: топ по 24h росту",
        "",
        f"- Окно: **{start.strftime('%d.%m %Y %H:%M')} – {end.strftime('%d.%m %Y %H:%M')}** YEKT",
        f"- Equity: **${args.equity:.0f}**, щуп **${cfg().probe_usd:.0f}×{PROBE_MAX_LEGS}**",
        f"- Universe: **top {args.top}** gainers на старте из pool **{len(download_list)}** (turnover ≥ ${args.min_turnover:,.0f})",
        f"- Scan: сортировка по **24h %**, первые **{PROBE_SCAN_MANDATORY_TOP}** обязательно в очереди щупа",
        "",
        "## Топ монет на старте окна",
        "",
        "| # | Ticker | day24 % |",
        "|---|--------|---------|",
    ]
    for i, (chg, t) in enumerate(top_start, 1):
        lines.append(f"| {i} | {t} | {chg:+.2f} |")
    lines.extend(["", "## Итоги вариантов (realized)", "", "| Вариант | Realized | Buys | Finish |", "|---------|----------|------|--------|"])
    for r in results:
        lines.append(
            f"| {r['variant']} | **${r['realized']:+.2f}** | {r['buys']} | ${r['finish']:.2f} |"
        )
    best = results[0]
    lines.extend(["", f"**Лучший:** `{best['variant']}` → ${best['realized']:+.2f}", "", "## Сделки лучшего варианта", ""])
    for tr in best.get("trades") or []:
        extra = f" {tr['reason']}" if tr.get("reason") else ""
        pnl = f" ${tr['pnl']:+.2f}" if tr["side"] == "sell" else ""
        lines.append(f"- {tr['t']} {tr['side']} **{tr['ticker']}** [{tr['algo']}]{pnl}{extra}")

    path = str(args.out)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nЗаписано: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
