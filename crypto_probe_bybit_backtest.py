#!/usr/bin/env python3
"""Прогон щупа по живым правилам стола на свечах Bybit.

Пороги не скопированы: вход, тренд и выход берутся из desk_policy.
Поменяли стол — следующий запуск уже на новых правилах.

Список монет — текущая лента, та же сортировка, что у скана щупа.

    PYTHONPATH=. .venv/bin/python crypto_probe_bybit_backtest.py
    PYTHONPATH=. .venv/bin/python crypto_probe_bybit_backtest.py --top 6 --limit 1000
    PYTHONPATH=. .venv/bin/python crypto_probe_bybit_backtest.py --symbols SUI,SOL,DOGE
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from skills.crypto import desk_policy as P

YEKT = timezone(timedelta(hours=5))
API = "https://api.bybit.com/v5/market"
FEE_PCT = 2.0 * P.EXIT_FEE_SIDE * 100.0


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-probe-bt/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, YEKT).strftime("%m-%d %H:%M")


def fetch_klines(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    url = f"{API}/kline?category=spot&symbol={symbol}&interval={interval}&limit={limit}"
    raw = (_get(url).get("result") or {}).get("list") or []
    bars: list[dict[str, Any]] = []
    for item in reversed(raw):
        raw_ts = float(item[0])
        bars.append({
            "ts": raw_ts / 1000.0 if raw_ts > 1e11 else raw_ts,
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        })
    return bars


def live_tape() -> list[dict[str, Any]]:
    """Спотовые USDT в форме строки скана щупа."""
    rows = (_get(f"{API}/tickers?category=spot").get("result") or {}).get("list") or []
    out: list[dict[str, Any]] = []
    for item in rows:
        sym = str(item.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if not base or base in P.SLEEVE_SKIP or P._leveraged_base(base):
            continue
        try:
            turn = float(item.get("turnover24h") or 0)
            chg = float(item.get("price24hPcnt") or 0) * 100.0
            px = float(item.get("lastPrice") or 0)
            high = float(item.get("highPrice24h") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out.append({
            "ticker": base,
            "turnover": turn,
            "chg": chg,
            "price": px,
            "high": high,
        })
    return out


def pick_symbols(top: int, explicit: list[str] | None) -> list[str]:
    if explicit:
        return [name.strip().upper() for name in explicit if name.strip()]
    ranked = P.probe_scan_ranked_rows(live_tape(), limit=top)
    return [str(row["ticker"]) for _score, row in ranked[:top]]


def _week_chg(bars60: list[dict[str, Any]], price: float) -> float | None:
    if len(bars60) < 24 * 7 or price <= 0:
        return None
    old = float(bars60[-24 * 7].get("close") or 0)
    if old <= 0:
        return None
    return (price / old - 1.0) * 100.0


def run_symbol(symbol: str, limit: int, usd: float) -> dict[str, Any]:
    bars5 = fetch_klines(symbol, "5", limit)
    bars15 = fetch_klines(symbol, "15", min(1000, limit // 3 + 40))
    bars60 = fetch_klines(symbol, "60", 200)
    trades: list[dict[str, Any]] = []
    handoffs: list[dict[str, Any]] = []
    if len(bars5) < 30:
        return {"symbol": symbol, "trades": trades, "handoffs": handoffs, "note": "мало свечей"}

    in_position = False
    entry = 0.0
    algo = ""
    shelf = 0.0
    peak = 0.0
    opened = 0.0
    shelf_block_until = 0.0
    for i in range(25, len(bars5)):
        window5 = bars5[: i + 1]
        cur = window5[-1]
        now_ts = float(cur["ts"]) + 90.0
        price = float(cur["close"])
        w15 = [b for b in bars15 if b["ts"] <= now_ts]
        w60 = [b for b in bars60 if b["ts"] <= now_ts]
        trend15 = P.ma_trend([b["close"] for b in w15])
        trend1h = P.ma_trend([b["close"] for b in w60])
        if not in_position:
            if now_ts < shelf_block_until:
                continue
            day = window5[-288:]
            day_high = max(float(b["high"]) for b in day)
            first_open = float(day[0]["open"])
            day_chg = (price / first_open - 1.0) * 100.0 if first_open > 0 else 0.0
            setup = P.probe_pick_setup(
                w15,
                window5,
                day_chg=day_chg,
                day_high=day_high,
                now=now_ts,
            )
            if not setup:
                continue
            name = str(setup.get("algo") or "")
            if not P.probe_trends_ok(
                trend15, trend1h, day_chg, week_chg=_week_chg(w60, price), algo=name
            ):
                continue
            in_position = True
            entry = price
            algo = name
            shelf = float(setup.get("shelf") or cur["open"])
            peak = float(cur["high"])
            opened = now_ts
            continue
        peak = max(peak, float(cur["high"]))
        reason = P.probe_exit_reason(
            price=price,
            entry=entry,
            shelf=shelf,
            trend15=trend15,
            peak=peak,
            algo=algo,
            qty=usd / entry if entry > 0 else 0.0,
            opened_ts=opened,
            now=now_ts,
            bid=price * (1.0 - 0.0005),
        )
        if not reason:
            continue
        gross = (price / entry - 1.0) * 100.0
        row = {
            "algo": algo,
            "reason": reason,
            "gross": gross,
            "net": gross - FEE_PCT,
            "usd": (gross - FEE_PCT) / 100.0 * usd,
            "t_in": opened,
            "t_out": now_ts,
        }
        if P.probe_should_handoff(reason):
            handoffs.append(row)
        else:
            trades.append(row)
        if reason == "полка":
            shelf_block_until = now_ts + float(P.PROBE_SHELF_REENTRY_SEC)
        in_position = False
    return {
        "symbol": symbol,
        "trades": trades,
        "handoffs": handoffs,
        "open": in_position,
        "span": (_fmt(bars5[0]["ts"]), _fmt(bars5[-1]["ts"])),
    }


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    print(f"  {title}")
    for row in rows:
        print(
            f"    {_fmt(row['t_in'])}→{_fmt(row['t_out'])} {row['algo']:8} {row['reason']:8} "
            f"gross {row['gross']:+.2f}% net {row['net']:+.2f}% (${row['usd']:+.2f})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон щупа по живым правилам стола")
    parser.add_argument("--top", type=int, default=6, help="Сколько имён взять из скана")
    parser.add_argument("--limit", type=int, default=1000, help="Сколько 5м свечей, максимум Bybit 1000")
    parser.add_argument("--symbols", type=str, default="", help="Явный список через запятую, без USDT")
    args = parser.parse_args()
    explicit = [part for part in args.symbols.split(",") if part.strip()] or None
    symbols = pick_symbols(max(1, args.top), explicit)
    usd = float(P.cfg().probe_usd or P.PROBE_USD)
    print(
        "Щуп, живые правила: "
        f"тело {P.probe_candle_min_body_pct():.1f}–{P.probe_candle_max_body_pct():.1f}%, "
        f"pop {P.PROBE_POP_MIN_BODY_PCT:.1f}–{P.PROBE_POP_MAX_BODY_PCT:.1f}%, "
        f"откат подскока {P.PROBE_FAST_TRAP_PULLBACK_PCT:.2f}% "
        f"(взвод +${P.PROBE_FAST_TRAP_EXTRA_USD:.2f}, "
        f"убыток ≤ −{P.PROBE_FAST_LOSS_CAP_PCT:.1f}%, "
        f"после полки пауза {P.PROBE_SHELF_REENTRY_SEC / 60:.0f}м), "
        f"(от +{P.PROBE_FAST_TRAP_RUN_PCT:.0f}% → {P.PROBE_FAST_TRAP_RUN_PULLBACK_PCT:.1f}%), "
        f"пин {P.PROBE_PIN_MOVE_BARS}×5м, ширина ≥{P.PROBE_MIN_MARKET_BREADTH_PCT:.0f}%, "
        f"нога ${usd:.0f}, комиссия {FEE_PCT:.2f}%."
    )
    print("Монеты:", ", ".join(symbols))
    sold_usd = 0.0
    sold_n = 0
    wins = 0
    for base in symbols:
        symbol = base if base.endswith("USDT") else f"{base}USDT"
        print(f"\n=== {base} ===")
        try:
            result = run_symbol(symbol, min(1000, max(50, args.limit)), usd)
        except Exception as exc:
            print(f"  не загрузилось: {exc}")
            continue
        span = result.get("span")
        if span:
            print(f"  {span[0]} — {span[1]} YEKT")
        if result.get("note"):
            print(f"  {result['note']}")
        _print_rows("продажи", result["trades"])
        _print_rows("в карман2, не продажа", result["handoffs"])
        if result.get("open"):
            print("  нога ещё открыта на конце окна")
        if not result["trades"] and not result["handoffs"] and not result.get("open"):
            print("  входов не было")
        for row in result["trades"]:
            sold_n += 1
            sold_usd += float(row["usd"])
            if row["usd"] > 0:
                wins += 1
    print("\n" + "=" * 45)
    if sold_n:
        print(f"Продаж: {sold_n}, в плюсе {wins}/{sold_n}, чистыми ${sold_usd:+.2f}")
    else:
        print("Закрытых продаж нет.")
    print("Передачи в карман2 в эту сумму не входят.")


if __name__ == "__main__":
    main()
