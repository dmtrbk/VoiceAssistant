#!/usr/bin/env python3
"""Полный прогон стола за сутки: карман + рукав + щуп (2 ноги).

Стартуем с кэша = equity. Шаг — 5м свечи Bybit. Комиссия taker 0.18%/сторона.

    .venv/bin/python crypto_desk_full_replay.py
    .venv/bin/python crypto_desk_full_replay.py --hours 24 --equity 404
"""

from __future__ import annotations

import argparse
import bisect
import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from skills.crypto import desk_policy as P
from skills.crypto.desk_policy import (
    ALT_BB_INTERVAL,
    BTC_DIP_TICKER,
    DESK_ALT_SLEEVE,
    PROBE_MAX_LEGS,
    PROBE_SCAN_LIMIT,
    alt_exit_reason,
    alt_slot_pct,
    btc_dip_exit_reason,
    btc_dip_quote,
    cfg,
    is_risk_off,
    pocket_bounce_ready,
    pocket_dip_held,
    pocket_dip_pending,
    pocket_swing,
    probe_arm_action,
    probe_exit_reason,
    probe_pick_setup,
    probe_quote,
    probe_reserve_quote,
    probe_scan_rank,
    probe_trends_ok,
    second_pocket_choice,
    sui_dip_quote,
    update_alt_arm,
    update_alt_exit_arm,
    update_exclusions,
    update_pocket_exit_arm,
    update_pocket_swing_arm,
)
from skills.crypto.indicators import bollinger

YEKT = timezone(timedelta(hours=5))
FEE = 0.0018
API = "https://api.bybit.com/v5/market"


def get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-desk-full-replay/1"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            f"{API}/kline?category=spot&symbol={symbol}"
            f"&interval={interval}&start={cursor}&end={end_ms}&limit=1000"
        )
        rows = (get(url).get("result") or {}).get("list") or []
        if not rows:
            break
        for row in reversed(rows):
            ts = int(row[0])
            out.append(
                {
                    "ts": ts / 1000.0,
                    "t": datetime.fromtimestamp(ts / 1000.0, YEKT),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        last = int(rows[0][0])
        if last + 1 <= cursor:
            break
        cursor = last + 1
        if len(rows) < 1000:
            break
        time.sleep(0.02)
    seen = {b["ts"]: b for b in out}
    return [seen[k] for k in sorted(seen)]


def as_ohlc(rows: list[dict[str, Any]]) -> list[dict[str, float]]:
    return [
        {"high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]}
        for b in rows
    ]


def day_chg_at(bars15: list[dict[str, Any]], ts: float, price: float) -> float | None:
    if not bars15 or price <= 0:
        return None
    ts15 = [b["ts"] for b in bars15]
    k = bisect.bisect_left(ts15, ts - 86400.0)
    if k >= len(bars15):
        return None
    day0 = float(bars15[k]["close"] or 0)
    if day0 <= 0:
        return None
    return (price / day0 - 1.0) * 100.0


def week_chg_at(bars60: list[dict[str, Any]], ts: float, price: float) -> float | None:
    if not bars60 or price <= 0:
        return None
    ts60 = [b["ts"] for b in bars60]
    k = bisect.bisect_left(ts60, ts - 7 * 86400.0)
    if k >= len(bars60):
        return None
    week0 = float(bars60[k]["close"] or 0)
    if week0 <= 0:
        return None
    return (price / week0 - 1.0) * 100.0


def window_upto(bars: list[dict[str, Any]], ts: float, need: int) -> list[dict[str, Any]]:
    if not bars:
        return []
    ts_list = [b["ts"] for b in bars]
    j = bisect.bisect_right(ts_list, ts)
    if j <= 0:
        return []
    return bars[max(0, j - need) : j]


def px_at(bars: list[dict[str, Any]], ts: float) -> float:
    w = window_upto(bars, ts, 2)
    return float(w[-1]["close"]) if w else 0.0


def bb_z_at(bars240: list[dict[str, Any]], ts: float, price: float) -> float | None:
    w = window_upto(bars240, ts, 30)
    if len(w) < 20 or price <= 0:
        return None
    closes = [float(b["close"]) for b in w[:-1]] + [price]
    band = bollinger(closes)
    return None if not band else float(band["z"])


@dataclass
class Leg:
    kind: str  # pocket / second / sleeve / probe
    ticker: str
    qty: float
    entry: float
    opened_ts: float
    algo: str = ""
    shelf: float = 0.0
    arm_px: float = 0.0
    peak: float = 0.0
    low: float = 0.0
    exit_arm: dict[str, float] | None = None
    swing_low: float = 0.0


@dataclass
class Arm:
    kind: str
    ticker: str
    ts: float
    low: float = 0.0
    shelf: float = 0.0
    px: float = 0.0
    algo: str = ""
    hold_sec: float = 0.0
    chase_pct: float = 0.0


@dataclass
class Book:
    cash: float
    start_equity: float
    legs: list[Leg] = field(default_factory=list)
    pocket_arm: dict[str, float] | None = None
    second_arm: dict[str, float] | None = None
    sleeve_arms: dict[str, dict[str, float]] = field(default_factory=dict)
    probe_arms: list[Arm] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    realized: float = 0.0
    fees: float = 0.0

    def mtm(self, prices: dict[str, float]) -> float:
        total = self.cash
        for leg in self.legs:
            px = float(prices.get(leg.ticker) or 0)
            if px > 0:
                total += leg.qty * px
        return total

    def open_kinds(self, kind: str) -> list[Leg]:
        return [leg for leg in self.legs if leg.kind == kind]

    def has_ticker(self, ticker: str) -> bool:
        return any(leg.ticker == ticker for leg in self.legs)

    def probe_count(self) -> int:
        return len(self.open_kinds("probe")) + len(self.probe_arms)

    def log(
        self,
        ts: float,
        side: str,
        ticker: str,
        *,
        kind: str,
        px: float,
        quote: float,
        pnl: float = 0.0,
        reason: str = "",
        algo: str = "",
    ) -> None:
        self.events.append(
            {
                "t": datetime.fromtimestamp(ts, YEKT),
                "side": side,
                "ticker": ticker,
                "kind": kind,
                "px": px,
                "quote": quote,
                "pnl": pnl,
                "reason": reason,
                "algo": algo,
            }
        )


def buy(
    book: Book,
    *,
    ts: float,
    kind: str,
    ticker: str,
    price: float,
    quote: float,
    algo: str = "",
    shelf: float = 0.0,
    arm_px: float = 0.0,
    low: float = 0.0,
) -> bool:
    if price <= 0 or quote <= 0 or book.cash + 1e-9 < quote:
        return False
    cost = quote * (1.0 + FEE)
    if book.cash + 1e-9 < cost:
        cost = book.cash
        quote = cost / (1.0 + FEE)
    if quote < 5.0:
        return False
    qty = quote / price
    book.cash -= cost
    book.fees += quote * FEE
    book.legs.append(
        Leg(
            kind=kind,
            ticker=ticker,
            qty=qty,
            entry=price * (1.0 + FEE),
            opened_ts=ts,
            algo=algo,
            shelf=shelf,
            arm_px=arm_px or price,
            peak=price,
            low=low or price,
        )
    )
    book.log(ts, "buy", ticker, kind=kind, px=price, quote=quote, algo=algo)
    return True


def sell(book: Book, leg: Leg, *, ts: float, price: float, reason: str) -> None:
    if price <= 0 or leg.qty <= 0:
        return
    proceeds = leg.qty * price * (1.0 - FEE)
    basis = leg.qty * leg.entry
    pnl = proceeds - basis
    book.cash += proceeds
    book.fees += leg.qty * price * FEE
    book.realized += pnl
    book.log(
        ts,
        "sell",
        leg.ticker,
        kind=leg.kind,
        px=price,
        quote=proceeds,
        pnl=pnl,
        reason=reason,
        algo=leg.algo,
    )
    book.legs = [item for item in book.legs if item is not leg]


def free_cash(book: Book, equity: float) -> float:
    """Кэш минус резервы под пустые карман/щуп — как idle_special_reserve."""
    pocket_open = bool(book.open_kinds("pocket"))
    second_open = bool(book.open_kinds("second"))
    reserve = 0.0
    if not pocket_open:
        reserve += btc_dip_quote(equity)
    if not second_open and cfg().second_on:
        reserve += sui_dip_quote()
    reserve += probe_reserve_quote(
        open_legs=len(book.open_kinds("probe")),
        pending=len(book.probe_arms),
    )
    return max(0.0, book.cash - reserve)


def build_scan(extra: list[str]) -> list[dict[str, Any]]:
    tape = (get(f"{API}/tickers?category=spot").get("result") or {}).get("list") or []
    ranked: list[tuple[float, str, float]] = []
    for item in tape:
        sym = str(item.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in P.SLEEVE_SKIP or P._leveraged_base(base):
            continue
        try:
            row = {
                "chg": float(item.get("price24hPcnt") or 0) * 100.0,
                "price": float(item.get("lastPrice") or 0),
                "high": float(item.get("highPrice24h") or 0),
                "low": float(item.get("lowPrice24h") or 0),
                "turnover": float(item.get("turnover24h") or 0),
            }
        except (TypeError, ValueError):
            continue
        rank = probe_scan_rank(row)
        if rank < 0:
            continue
        ranked.append((rank, base, row["chg"]))
    ranked.sort(reverse=True)
    scan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _rank, base, chg in ranked:
        if base in seen:
            continue
        seen.add(base)
        scan.append({"ticker": base, "symbol": base + "USDT", "chg": chg})
        if len(scan) >= 20:
            break
    for name in extra:
        if name not in seen:
            scan.append({"ticker": name, "symbol": name + "USDT", "chg": 0.0})
            seen.add(name)
    return scan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--equity", type=float, default=404.0)
    parser.add_argument(
        "--second",
        default="",
        help="Второй карман: AVA/SUI. Пусто — из настроек, но для суток AVA если в сделках.",
    )
    args = parser.parse_args()

    limits = cfg()
    end = datetime.now(YEKT).replace(second=0, microsecond=0)
    start = end - timedelta(hours=args.hours)
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    second = (args.second or "AVA").upper()  # сутки шли на AVA, не на SUI из settings
    if second not in {"AVA", "SUI"}:
        second = second_pocket_choice()
    probe_usd = float(limits.probe_usd)
    slot_pct = alt_slot_pct()

    print(
        f"Окно {start.strftime('%d.%m %H:%M')}–{end.strftime('%d.%m %H:%M')} YEKT | "
        f"equity ${args.equity:.0f} | probe ${probe_usd:.0f}×{PROBE_MAX_LEGS} | "
        f"карман SOL {limits.pocket_pct:.0f}% + {second} ${sui_dip_quote():.0f} | "
        f"рукав {limits.sleeve_max_pct:.0f}%/{limits.sleeve_max_names}"
    )

    scan = build_scan(["PONS", "PEPE", "GRASS", "DOOD", "ARX", "XAN", "HAEDAL", "GROVE"])
    print("Щуп-лента:", ", ".join(f"{r['ticker']} {r['chg']:+.1f}%" for r in scan[:8]))

    warm_ms = int((start - timedelta(days=10)).timestamp() * 1000)
    end_ms = int(end_ts * 1000)

    need_bars = {
        BTC_DIP_TICKER,
        second,
        "BTC",
        *DESK_ALT_SLEEVE,
        *[r["ticker"] for r in scan],
    }
    series: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for n, ticker in enumerate(sorted(need_bars), 1):
        sym = ticker + "USDT"
        try:
            series[ticker] = {
                "5": klines(sym, "5", warm_ms, end_ms),
                "15": klines(sym, "15", warm_ms, end_ms),
                "60": klines(sym, "60", warm_ms, end_ms),
                "240": klines(sym, ALT_BB_INTERVAL, warm_ms, end_ms),
            }
        except Exception as exc:
            print(f"skip {ticker}: {exc}")
            continue
        if n % 10 == 0:
            print(f"  свечи …{n}/{len(need_bars)}")

    # Сетка 5м по SOL (якорное время)
    grid = [
        b["ts"]
        for b in series.get(BTC_DIP_TICKER, {}).get("5", [])
        if start_ts <= b["ts"] <= end_ts
    ]
    if not grid:
        print("Нет сетки времени")
        return 1

    book = Book(cash=float(args.equity), start_equity=float(args.equity))
    blacklist = {"GRAM"}
    excluded: set[str] = set()

    def prices_now(ts: float) -> dict[str, float]:
        out: dict[str, float] = {}
        for ticker, bars in series.items():
            px = px_at(bars["5"] or bars["15"], ts)
            if px > 0:
                out[ticker] = px
        return out

    for step, ts in enumerate(grid):
        prices = prices_now(ts)
        equity = max(book.mtm(prices), 1.0)
        btc_day = day_chg_at(series["BTC"]["15"], ts, prices.get("BTC", 0))
        btc_week = week_chg_at(series["BTC"]["60"], ts, prices.get("BTC", 0))
        risk = is_risk_off(btc_chg_7=btc_week, btc_chg_day=btc_day)

        # --- exclusions по неделе ---
        rows_ex = []
        for ticker in DESK_ALT_SLEEVE:
            if ticker not in series:
                continue
            px = prices.get(ticker) or 0
            w = week_chg_at(series[ticker]["60"], ts, px)
            rows_ex.append({"ticker": ticker, "chg_7": w})
        excluded = update_exclusions(excluded, rows_ex)

        # --- выходы щупа ---
        for leg in list(book.open_kinds("probe")):
            bars15 = window_upto(series[leg.ticker]["15"], ts, 130)
            if len(bars15) < 50:
                continue
            price = prices.get(leg.ticker) or 0
            if price <= 0:
                continue
            leg.peak = max(leg.peak, price)
            trend = P.ma_trend([b["close"] for b in bars15])
            reason = probe_exit_reason(
                price=price,
                entry=leg.entry,
                shelf=leg.shelf,
                trend15=trend,
                arm_px=leg.arm_px,
                peak=leg.peak,
                entry_low=leg.low,
                algo=leg.algo,
            )
            if reason:
                sell(book, leg, ts=ts, price=price, reason=reason)

        # --- выходы карманов ---
        for kind, arm_attr in (("pocket", "pocket_arm"), ("second", "second_arm")):
            for leg in list(book.open_kinds(kind)):
                price = prices.get(leg.ticker) or 0
                if price <= 0:
                    continue
                leg.peak = max(leg.peak, price)
                leg.exit_arm = update_pocket_exit_arm(
                    leg.exit_arm, price=price, entry=leg.entry, now=ts
                )
                reason = btc_dip_exit_reason(
                    price=price,
                    entry=leg.entry,
                    exit_arm=leg.exit_arm,
                    now=ts,
                    swing_low=leg.swing_low,
                )
                if reason:
                    sell(book, leg, ts=ts, price=price, reason=reason)
                    setattr(book, arm_attr, None)

        # --- выходы рукава ---
        for leg in list(book.open_kinds("sleeve")):
            price = prices.get(leg.ticker) or 0
            if price <= 0:
                continue
            leg.exit_arm = update_alt_exit_arm(
                leg.exit_arm, price=price, entry=leg.entry, now=ts
            )
            reason = alt_exit_reason(
                price=price,
                entry=leg.entry,
                exit_arm=leg.exit_arm,
                now=ts,
                swing_low=leg.swing_low,
            )
            if reason:
                sell(book, leg, ts=ts, price=price, reason=reason)
                book.sleeve_arms.pop(leg.ticker, None)

        equity = max(book.mtm(prices), 1.0)

        # --- карман SOL ---
        if BTC_DIP_TICKER in series:
            sol_px = prices.get(BTC_DIP_TICKER) or 0
            bars15 = window_upto(series[BTC_DIP_TICKER]["15"], ts, 40)
            if sol_px > 0 and bars15:
                day = day_chg_at(series[BTC_DIP_TICKER]["15"], ts, sol_px)
                swing = pocket_swing(as_ohlc(bars15), pullback_pct=P.POCKET_SWING_PCT)
                keep = False
                if book.pocket_arm and float(book.pocket_arm.get("low") or 0) > 0:
                    keep = pocket_dip_pending(as_ohlc(bars15), float(book.pocket_arm["low"]))
                book.pocket_arm = update_pocket_swing_arm(
                    book.pocket_arm,
                    price=sol_px,
                    swing_low=swing,
                    now=ts,
                    keep=keep,
                )
                if (
                    not book.open_kinds("pocket")
                    and book.pocket_arm
                    and pocket_bounce_ready(book.pocket_arm, price=sol_px, now=ts)
                    and pocket_dip_held(as_ohlc(bars15), float(book.pocket_arm["low"]))
                ):
                    quote = min(btc_dip_quote(equity), book.cash * 0.998)
                    if buy(
                        book,
                        ts=ts,
                        kind="pocket",
                        ticker=BTC_DIP_TICKER,
                        price=sol_px,
                        quote=quote,
                        low=float(book.pocket_arm["low"]),
                    ):
                        book.legs[-1].swing_low = float(book.pocket_arm["low"])
                        book.pocket_arm = None

        # --- второй карман ---
        if second in series and cfg().second_on:
            px = prices.get(second) or 0
            bars15 = window_upto(series[second]["15"], ts, 40)
            if px > 0 and bars15:
                swing = pocket_swing(as_ohlc(bars15), pullback_pct=P.POCKET_SWING_PCT)
                keep = False
                if book.second_arm and float(book.second_arm.get("low") or 0) > 0:
                    keep = pocket_dip_pending(as_ohlc(bars15), float(book.second_arm["low"]))
                book.second_arm = update_pocket_swing_arm(
                    book.second_arm,
                    price=px,
                    swing_low=swing,
                    now=ts,
                    keep=keep,
                )
                if (
                    not book.open_kinds("second")
                    and book.second_arm
                    and pocket_bounce_ready(book.second_arm, price=px, now=ts)
                    and pocket_dip_held(as_ohlc(bars15), float(book.second_arm["low"]))
                ):
                    quote = min(sui_dip_quote(), book.cash * 0.998)
                    if buy(
                        book,
                        ts=ts,
                        kind="second",
                        ticker=second,
                        price=px,
                        quote=quote,
                        low=float(book.second_arm["low"]),
                    ):
                        book.legs[-1].swing_low = float(book.second_arm["low"])
                        book.second_arm = None

        # --- рукав: взвод и вход ---
        sleeve_held = {leg.ticker for leg in book.open_kinds("sleeve")}
        room = max(0, int(limits.sleeve_max_names) - len(sleeve_held))
        if not risk and room > 0:
            candidates: list[tuple[float, str, float]] = []
            for ticker in DESK_ALT_SLEEVE:
                if ticker in sleeve_held or ticker in blacklist or ticker in excluded:
                    continue
                if ticker in {BTC_DIP_TICKER, second}:
                    # карманные тикеры рукав не набирает, пока карман может их занять
                    continue
                if ticker not in series:
                    continue
                price = prices.get(ticker) or 0
                z = bb_z_at(series[ticker]["240"], ts, price)
                updated = update_alt_arm(
                    book.sleeve_arms.get(ticker),
                    price=price,
                    z=z,
                    now=ts,
                )
                if updated is None:
                    book.sleeve_arms.pop(ticker, None)
                else:
                    book.sleeve_arms[ticker] = updated
                if updated and P.alt_bounce_ready(updated, price=price, z=z, now=ts):
                    candidates.append((float(z), ticker, price))
            candidates.sort()
            equity = max(book.mtm(prices), 1.0)
            avail = free_cash(book, equity)
            for _z, ticker, price in candidates[:room]:
                target = equity * (slot_pct / 100.0)
                quote = min(target, avail * 0.998)
                arm = book.sleeve_arms.get(ticker) or {}
                if buy(
                    book,
                    ts=ts,
                    kind="sleeve",
                    ticker=ticker,
                    price=price,
                    quote=quote,
                    low=float(arm.get("low") or price),
                ):
                    book.legs[-1].swing_low = float(arm.get("low") or 0)
                    book.sleeve_arms.pop(ticker, None)
                    avail = free_cash(book, max(book.mtm(prices), 1.0))
                    room -= 1
                    if room <= 0:
                        break

        # --- щуп: ведём взводы ---
        still_arms: list[Arm] = []
        for arm in book.probe_arms:
            if arm.ticker not in series:
                continue
            bars15 = window_upto(series[arm.ticker]["15"], ts, 130)
            if len(bars15) < 50:
                still_arms.append(arm)
                continue
            price = prices.get(arm.ticker) or 0
            if price <= 0:
                still_arms.append(arm)
                continue
            trend = P.ma_trend([b["close"] for b in bars15])
            day = day_chg_at(series[arm.ticker]["15"], ts, price)
            week = week_chg_at(series[arm.ticker]["60"], ts, price)
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
                book.log(
                    ts,
                    "drop",
                    arm.ticker,
                    kind="probe",
                    px=price,
                    quote=0.0,
                    algo=arm.algo,
                    reason="срыв",
                )
                continue
            # buy
            if book.cash < probe_usd * 0.5:
                still_arms.append(arm)
                continue
            if len(book.open_kinds("probe")) >= PROBE_MAX_LEGS:
                continue
            bars5 = window_upto(series[arm.ticker]["5"], ts, 5)
            low = float(bars5[-1]["low"]) if bars5 else price
            if buy(
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

        # --- щуп: скан новых ---
        if book.probe_count() < PROBE_MAX_LEGS:
            blocked = {
                leg.ticker for leg in book.legs
            } | {arm.ticker for arm in book.probe_arms} | blacklist | set(DESK_ALT_SLEEVE)
            # рукав не блок для щупа в live — только SLEEVE_SKIP. Уберём DESK блок.
            blocked = {
                leg.ticker for leg in book.legs
            } | {arm.ticker for arm in book.probe_arms} | blacklist | set(P.SLEEVE_SKIP)

            ranked_now: list[tuple[float, str]] = []
            for row in scan:
                ticker = row["ticker"]
                if ticker in blocked or ticker not in series:
                    continue
                price = prices.get(ticker) or 0
                day = day_chg_at(series[ticker]["15"], ts, price) or 0.0
                # turnover proxy: live rank uses tape; здесь day% как грубый rank
                bars15 = window_upto(series[ticker]["15"], ts, 100)
                if len(bars15) < 10 or price <= 0:
                    continue
                high = max(float(b["high"]) for b in bars15[-96:]) if len(bars15) >= 10 else price
                low = min(float(b["low"]) for b in bars15[-96:]) if len(bars15) >= 10 else price
                turn = 2_000_000.0  # выше порога; точный оборот на истории не восстанавливаем
                rank = probe_scan_rank(
                    {"chg": day, "price": price, "high": high, "low": low, "turnover": turn}
                )
                if rank < 0:
                    continue
                ranked_now.append((rank, ticker))
            ranked_now.sort(reverse=True)
            for _rank, ticker in ranked_now[:PROBE_SCAN_LIMIT]:
                if book.probe_count() >= PROBE_MAX_LEGS:
                    break
                bars15 = window_upto(series[ticker]["15"], ts, 130)
                bars5 = window_upto(series[ticker]["5"], ts, 90)
                if len(bars15) < 60 or len(bars5) < 20:
                    continue
                price = prices.get(ticker) or 0
                day = day_chg_at(series[ticker]["15"], ts, price)
                week = week_chg_at(series[ticker]["60"], ts, price)
                setup = probe_pick_setup(as_ohlc(bars15), as_ohlc(bars5), day_chg=day)
                if not setup:
                    continue
                trend = P.ma_trend([b["close"] for b in bars15])
                hour = [bars15[n]["close"] for n in range(0, len(bars15), 4)]
                t1h = P.ma_trend(hour) if len(hour) >= 50 else "neutral"
                if not probe_trends_ok(trend, t1h, day, week_chg=week):
                    continue
                book.probe_arms.append(
                    Arm(
                        kind="probe",
                        ticker=ticker,
                        ts=ts,
                        shelf=float(setup["shelf"]),
                        px=float(setup["price"]),
                        algo=str(setup["algo"]),
                        hold_sec=float(setup.get("hold_sec") or 0),
                        chase_pct=float(setup.get("chase_pct") or 0),
                    )
                )
                book.log(
                    ts,
                    "arm",
                    ticker,
                    kind="probe",
                    px=price,
                    quote=0.0,
                    algo=str(setup["algo"]),
                )
                break  # как live: один новый взвод за цикл

        if step and step % 40 == 0:
            print(
                f"  …{datetime.fromtimestamp(ts, YEKT).strftime('%d.%m %H:%M')} "
                f"eq ${book.mtm(prices):.0f} cash ${book.cash:.0f} "
                f"legs {len(book.legs)} realized ${book.realized:+.2f}"
            )

    # финал MTM
    prices = prices_now(end_ts)
    final_eq = book.mtm(prices)
    open_pnl = 0.0
    print("\n=== Итог полного стола ===")
    print(f"старт ${book.start_equity:.2f} → финиш ${final_eq:.2f} ({final_eq - book.start_equity:+.2f})")
    print(f"realized ${book.realized:+.2f} | fees ~${book.fees:.2f} | cash ${book.cash:.2f}")

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for ev in book.events:
        by_kind.setdefault(ev["kind"], []).append(ev)

    for kind in ("pocket", "second", "sleeve", "probe"):
        buys = [e for e in by_kind.get(kind, []) if e["side"] == "buy"]
        sells = [e for e in by_kind.get(kind, []) if e["side"] == "sell"]
        pnl_closed = sum(float(e["pnl"]) for e in sells)
        open_legs = book.open_kinds(kind if kind != "second" else "second")
        if kind == "second":
            open_legs = book.open_kinds("second")
        mtm = 0.0
        for leg in open_legs:
            px = prices.get(leg.ticker) or 0
            if px > 0:
                mtm += leg.qty * px * (1.0 - FEE) - leg.qty * leg.entry
        open_pnl += mtm
        label = {
            "pocket": "Карман SOL",
            "second": f"Карман {second}",
            "sleeve": "Рукав",
            "probe": "Щуп",
        }[kind]
        print(
            f"{label}: buy {len(buys)} / sell {len(sells)} / open {len(open_legs)} | "
            f"closed ${pnl_closed:+.2f} open_mtm ${mtm:+.2f}"
        )
        for e in buys + sells:
            extra = f" {e['reason']}" if e.get("reason") else ""
            algo = f" [{e['algo']}]" if e.get("algo") else ""
            pnl = f" {e['pnl']:+.2f}$" if e["side"] == "sell" else f" ${e['quote']:.0f}"
            print(
                f"  {e['t'].strftime('%d.%m %H:%M')} {e['side']:4} {e['ticker']:6}"
                f"{algo} @{e['px']:.6g}{pnl}{extra}"
            )
        for leg in open_legs:
            px = prices.get(leg.ticker) or 0
            pnl = leg.qty * px * (1.0 - FEE) - leg.qty * leg.entry if px else 0
            print(
                f"  OPEN {leg.ticker} @{leg.entry:.6g} → {px:.6g} "
                f"{pnl:+.2f}$ [{leg.algo or leg.kind}]"
            )

    print(f"\nВсего closed+open_mtm ${book.realized + open_pnl:+.2f}")
    print(
        f"(сравнение: щуп-only без лимита ног был ~+$62 при $30/нога; "
        f"здесь лимит {PROBE_MAX_LEGS} ног × ${probe_usd:.0f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
