#!/usr/bin/env python3
"""
Временный research: топ ликвидных USDT Perpetual (Bybit v5, category=linear).
Не трогает боевой стол. Публичный API, без ключей.

Запуск:
  .venv/bin/python research_linear_universe.py
  .venv/bin/python research_linear_universe.py --days 45 --top 30
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

HOST = "https://api.bybit.com"
STABLE_BASES = frozenset(
    {
        "USDT",
        "USDC",
        "USD",
        "DAI",
        "FDUSD",
        "TUSD",
        "USDE",
        "USDD",
        "BUSD",
        "PYUSD",
        "EUR",
        "EURC",
    }
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "VoiceAssistant-linear-research/1.0"})


def _get(path: str, params: dict[str, Any] | None = None, *, retries: int = 4) -> dict[str, Any]:
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            r = SESSION.get(f"{HOST}{path}", params=params or {}, timeout=30)
            if r.status_code in {429, 502, 503, 504}:
                time.sleep(0.6 * (2**attempt))
                continue
            r.raise_for_status()
            data = r.json()
            if int(data.get("retCode") or 0) != 0:
                raise RuntimeError(f"{path} retCode={data.get('retCode')} {data.get('retMsg')}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last = exc
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"GET {path} failed: {last}") from last


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    return float(statistics.median(xs))


def _mean(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    return float(statistics.fmean(xs))


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return float("nan")
    a = a[-n:]
    b = b[-n:]
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da < 1e-18 or db < 1e-18:
        return float("nan")
    return num / (da * db)


@dataclass
class Instrument:
    symbol: str
    base: str
    min_qty: float
    qty_step: float
    min_notional: float
    funding_interval_h: float


@dataclass
class Row:
    symbol: str
    base: str
    last: float = 0.0
    turnover24h: float = 0.0
    funding: float = 0.0
    funding_interval_h: float = 8.0
    min_qty: float = 0.0
    qty_step: float = 0.0
    min_notional: float = 0.0
    min_usd: float = 0.0
    spread_pct: float = float("nan")
    depth_10bps_usd: float = float("nan")
    depth_25bps_usd: float = float("nan")
    atr15_pct: float = float("nan")
    atr1h_pct: float = float("nan")
    er_4h: float = float("nan")
    er_12h: float = float("nan")
    wick15: float = float("nan")
    corr_btc: float = float("nan")
    flags: list[str] = field(default_factory=list)
    rets_1h: list[float] = field(default_factory=list, repr=False)


def fetch_instruments() -> dict[str, Instrument]:
    out: dict[str, Instrument] = {}
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _get("/v5/market/instruments-info", params)
        result = data.get("result") or {}
        for row in result.get("list") or []:
            if str(row.get("status") or "") != "Trading":
                continue
            if str(row.get("quoteCoin") or "").upper() != "USDT":
                continue
            if str(row.get("contractType") or "") not in {"LinearPerpetual", ""}:
                # Bybit: LinearPerpetual; пусто на всякий случай не режем
                if "Perpetual" not in str(row.get("contractType") or "Perpetual"):
                    continue
            base = str(row.get("baseCoin") or "").upper()
            if not base or base in STABLE_BASES:
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol.endswith("USDT"):
                continue
            lot = row.get("lotSizeFilter") or {}
            min_qty = _f(lot.get("minOrderQty"))
            qty_step = _f(lot.get("qtyStep"))
            min_notional = _f(lot.get("minNotionalValue"))
            # fundingInterval в минутах (часто 480 = 8h)
            fi_min = _f(row.get("fundingInterval"), 480.0)
            out[symbol] = Instrument(
                symbol=symbol,
                base=base,
                min_qty=min_qty,
                qty_step=qty_step,
                min_notional=min_notional,
                funding_interval_h=fi_min / 60.0 if fi_min else 8.0,
            )
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
        time.sleep(0.05)
    return out


def fetch_tickers() -> dict[str, dict[str, Any]]:
    data = _get("/v5/market/tickers", {"category": "linear"})
    out: dict[str, dict[str, Any]] = {}
    for row in data.get("result", {}).get("list") or []:
        sym = str(row.get("symbol") or "")
        if sym:
            out[sym] = row
    return out


def orderbook_metrics(symbol: str) -> tuple[float, float, float]:
    data = _get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 25})
    result = data.get("result") or {}
    bids = [(_f(p), _f(q)) for p, q in (result.get("b") or [])]
    asks = [(_f(p), _f(q)) for p, q in (result.get("a") or [])]
    if not bids or not asks:
        return float("nan"), float("nan"), float("nan")
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid <= 0:
        return float("nan"), float("nan"), float("nan")
    spread_pct = (best_ask - best_bid) / best_bid * 100.0
    mid = (best_bid + best_ask) / 2.0

    def depth_usd(levels: list[tuple[float, float]], *, side: str, bps: float) -> float:
        band = mid * (bps / 10000.0)
        total = 0.0
        for px, qty in levels:
            if side == "bid":
                if px < mid - band:
                    break
            else:
                if px > mid + band:
                    break
            total += px * qty
        return total

    d10 = depth_usd(bids, side="bid", bps=10) + depth_usd(asks, side="ask", bps=10)
    d25 = depth_usd(bids, side="bid", bps=25) + depth_usd(asks, side="ask", bps=25)
    return spread_pct, d10, d25


def fetch_klines(symbol: str, interval: str, days: int) -> list[list[Any]]:
    """Свечи oldest→newest. interval: '15' | '60'."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    all_rows: list[list[Any]] = []
    cursor_end = end_ms
    # Bybit limit max 1000
    while cursor_end > start_ms and len(all_rows) < days * 24 * (4 if interval == "15" else 1) + 50:
        data = _get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": start_ms,
                "end": cursor_end,
                "limit": 1000,
            },
        )
        batch = data.get("result", {}).get("list") or []
        if not batch:
            break
        # Bybit отдаёт newest first
        all_rows.extend(batch)
        oldest_ts = int(batch[-1][0])
        if oldest_ts <= start_ms:
            break
        cursor_end = oldest_ts - 1
        time.sleep(0.05)
    # unique by ts, sort ascending
    by_ts: dict[int, list[Any]] = {}
    for row in all_rows:
        by_ts[int(row[0])] = row
    rows = [by_ts[k] for k in sorted(by_ts)]
    return [r for r in rows if int(r[0]) >= start_ms]


def atr_pct(rows: list[list[Any]], period: int = 14) -> float:
    if len(rows) < period + 2:
        return float("nan")
    trs: list[float] = []
    prev_close = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if prev_close > 0:
            trs.append(tr / prev_close * 100.0)
        prev_close = close
    if len(trs) < period:
        return float("nan")
    # rolling ATR% then median of the series
    atr_series: list[float] = []
    window = trs[:period]
    atr = sum(window) / period
    atr_series.append(atr)
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        atr_series.append(atr)
    return _median(atr_series)


def efficiency_ratio(closes: list[float], bars: int) -> float:
    """ER на окне `bars`: |net| / sum(|delta|), усреднённый по сдвигам."""
    if len(closes) < bars + 5:
        return float("nan")
    ers: list[float] = []
    for i in range(bars, len(closes)):
        net = abs(closes[i] - closes[i - bars])
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - bars + 1, i + 1))
        if path < 1e-18:
            continue
        ers.append(net / path)
    return _mean(ers)


def wick_ratio_mean(rows: list[list[Any]]) -> float:
    vals: list[float] = []
    for row in rows:
        o, h, low, c = _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4])
        rng = h - low
        if rng <= 0:
            continue
        body = abs(c - o)
        vals.append((rng - body) / rng)
    return _mean(vals)


def hourly_returns(rows_1h: list[list[Any]]) -> list[float]:
    closes = [_f(r[4]) for r in rows_1h]
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        out.append(closes[i] / closes[i - 1] - 1.0)
    return out


def fmt(x: float, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Bybit linear USDT perp universe research")
    ap.add_argument("--days", type=int, default=45, help="окно свечей, дней (30–60)")
    ap.add_argument("--top", type=int, default=30, help="сколько топ по обороту брать")
    ap.add_argument("--min-turnover", type=float, default=50e6, help="мин. 24h turnover USD")
    ap.add_argument("--max-min-usd", type=float, default=10.0, help="макс. minOrder в USD")
    ap.add_argument("--max-spread", type=float, default=0.03, help="макс. спред %%")
    ap.add_argument(
        "--csv",
        default="research_linear_universe.csv",
        help="куда сохранить CSV",
    )
    args = ap.parse_args()
    days = max(30, min(60, args.days))

    print(f"=== Bybit linear research: {days}d, top {args.top} ===", flush=True)
    print("1) instruments-info…", flush=True)
    instruments = fetch_instruments()
    print(f"   perpetual USDT non-stable: {len(instruments)}", flush=True)

    print("2) tickers…", flush=True)
    tickers = fetch_tickers()

    candidates: list[tuple[float, str]] = []
    for sym, inst in instruments.items():
        t = tickers.get(sym)
        if not t:
            continue
        turnover = _f(t.get("turnover24h"))
        if turnover < args.min_turnover:
            continue
        candidates.append((turnover, sym))
    candidates.sort(reverse=True)
    top_syms = [s for _, s in candidates[: args.top]]
    if "BTCUSDT" not in top_syms and "BTCUSDT" in instruments:
        # нужен для корреляции
        top_syms.append("BTCUSDT")
    print(f"   candidates ≥ ${args.min_turnover/1e6:.0f}M: {len(candidates)}; analyzing {len(top_syms)}", flush=True)

    rows: dict[str, Row] = {}
    for i, sym in enumerate(top_syms, 1):
        inst = instruments[sym]
        t = tickers.get(sym) or {}
        last = _f(t.get("lastPrice"))
        row = Row(
            symbol=sym,
            base=inst.base,
            last=last,
            turnover24h=_f(t.get("turnover24h")),
            funding=_f(t.get("fundingRate")),
            funding_interval_h=inst.funding_interval_h,
            min_qty=inst.min_qty,
            qty_step=inst.qty_step,
            min_notional=inst.min_notional,
            min_usd=inst.min_qty * last if last > 0 else float("nan"),
        )
        if row.min_usd > args.max_min_usd:
            row.flags.append(f"min_usd>{args.max_min_usd:g}")
        if inst.min_notional > 0 and inst.min_notional > args.max_min_usd:
            row.flags.append(f"minNotional>{args.max_min_usd:g}")

        print(f"3) [{i}/{len(top_syms)}] {sym}: orderbook + klines…", flush=True)
        try:
            spread, d10, d25 = orderbook_metrics(sym)
            row.spread_pct, row.depth_10bps_usd, row.depth_25bps_usd = spread, d10, d25
            if not math.isnan(spread) and spread > args.max_spread:
                row.flags.append(f"spread>{args.max_spread:g}%")
        except Exception as exc:  # noqa: BLE001
            row.flags.append(f"ob_err:{exc}")

        try:
            k15 = fetch_klines(sym, "15", days)
            k1h = fetch_klines(sym, "60", days)
            closes15 = [_f(r[4]) for r in k15]
            closes1h = [_f(r[4]) for r in k1h]
            row.atr15_pct = atr_pct(k15)
            row.atr1h_pct = atr_pct(k1h)
            # 4ч = 16×15m; 12ч = 48×15m
            row.er_4h = efficiency_ratio(closes15, 16)
            row.er_12h = efficiency_ratio(closes15, 48)
            row.wick15 = wick_ratio_mean(k15)
            row.rets_1h = hourly_returns(k1h)
            if row.atr15_pct < 0.15:
                row.flags.append("low_ATR15")
            if row.er_4h < 0.12:
                row.flags.append("choppy_ER4h")
        except Exception as exc:  # noqa: BLE001
            row.flags.append(f"kline_err:{exc}")

        rows[sym] = row
        time.sleep(0.08)

    btc = rows.get("BTCUSDT")
    if btc and btc.rets_1h:
        for sym, row in rows.items():
            if sym == "BTCUSDT":
                row.corr_btc = 1.0
            elif row.rets_1h:
                row.corr_btc = _corr(row.rets_1h, btc.rets_1h)

    # --- table ---
    # Prefer tradeable for deposit: min_usd <= 10, then by turnover
    ranked = sorted(rows.values(), key=lambda r: (-r.turnover24h,))
    print("\n" + "=" * 140)
    print(
        f"{'SYMBOL':<12} {'TOUR$M':>7} {'LAST':>10} {'MIN$':>6} {'SPR%':>6} "
        f"{'D10$k':>7} {'D25$k':>7} {'FUND%':>7} {'ATR15%':>7} {'ATR1h%':>7} "
        f"{'ER4h':>5} {'ER12h':>5} {'WICK':>5} {'ρBTC':>5}  FLAGS"
    )
    print("-" * 140)
    for r in ranked:
        print(
            f"{r.symbol:<12} {r.turnover24h/1e6:7.1f} {r.last:10.4g} {fmt(r.min_usd, 2):>6} "
            f"{fmt(r.spread_pct, 4):>6} {fmt(r.depth_10bps_usd/1e3, 1):>7} {fmt(r.depth_25bps_usd/1e3, 1):>7} "
            f"{fmt(r.funding * 100, 4):>7} {fmt(r.atr15_pct, 3):>7} {fmt(r.atr1h_pct, 3):>7} "
            f"{fmt(r.er_4h, 3):>5} {fmt(r.er_12h, 3):>5} {fmt(r.wick15, 3):>5} {fmt(r.corr_btc, 2):>5}  "
            f"{','.join(r.flags) or '—'}"
        )

    # pairwise corr among tradeable (min_usd ok, no heavy flags on size)
    tradeable = [
        r
        for r in ranked
        if (not math.isnan(r.min_usd) and r.min_usd <= args.max_min_usd)
        and "minNotional" not in ",".join(r.flags)
    ]
    # keep BTC in matrix even if min_usd heavy — for reference only
    matrix_syms = [r.symbol for r in tradeable[:12]]
    if "BTCUSDT" in rows and "BTCUSDT" not in matrix_syms:
        matrix_syms = ["BTCUSDT"] + matrix_syms

    print("\n=== Pairwise corr (1h returns, 30–60d window) ===")
    hdr = f"{'':>12}" + "".join(f"{s.replace('USDT', ''):>8}" for s in matrix_syms)
    print(hdr)
    for a in matrix_syms:
        line = f"{a.replace('USDT', ''):>12}"
        for b in matrix_syms:
            if a == b:
                line += f"{'1.00':>8}"
            else:
                c = _corr(rows[a].rets_1h, rows[b].rets_1h)
                line += f"{fmt(c, 2):>8}"
        print(line)

    # --- scoring / recommendation ---
    print("\n=== Filters for $150–160 desk (leg ~$30–50) ===")
    print(
        f"keep: turnover≥${args.min_turnover/1e6:.0f}M, min_usd≤${args.max_min_usd:g}, "
        f"spread≤{args.max_spread:g}%, ATR15 preferably ≥0.15% (covers fees 0.04–0.11%)"
    )

    scored: list[tuple[float, Row]] = []
    for r in tradeable:
        if math.isnan(r.spread_pct) or r.spread_pct > args.max_spread:
            continue
        if math.isnan(r.atr15_pct) or math.isnan(r.er_4h):
            continue
        # score: liquidity + move quality + not too BTC-clone (but BTC itself special)
        fee_cover = min(r.atr15_pct / 0.11, 3.0)  # how many fee rounds in median 15m ATR
        depth_score = math.log10(max(r.depth_10bps_usd, 1.0))
        er_score = 0.55 * r.er_4h + 0.45 * (r.er_12h if not math.isnan(r.er_12h) else r.er_4h)
        wick_pen = max(0.0, (r.wick15 - 0.55) * 2.0) if not math.isnan(r.wick15) else 0.0
        # diversify: mild penalty for very high BTC corr (except BTC)
        corr_pen = 0.0
        if r.symbol != "BTCUSDT" and not math.isnan(r.corr_btc):
            corr_pen = max(0.0, r.corr_btc - 0.75) * 1.5
        score = (
            0.35 * min(r.turnover24h / 1e9, 3.0)
            + 0.25 * depth_score
            + 0.25 * fee_cover
            + 0.35 * er_score * 3.0
            - wick_pen
            - corr_pen
        )
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])

    print("\nTop scored tradeable:")
    for score, r in scored[:15]:
        print(
            f"  {r.symbol:<12} score={score:5.2f}  ATR15={fmt(r.atr15_pct)}%  "
            f"ER4h={fmt(r.er_4h)}  wick={fmt(r.wick15)}  ρBTC={fmt(r.corr_btc, 2)}  "
            f"min$={fmt(r.min_usd, 2)}  spr={fmt(r.spread_pct, 4)}%"
        )

    # pick 2–3 with low mutual corr and role diversity
    picks: list[Row] = []
    for _, r in scored:
        if len(picks) >= 3:
            break
        ok = True
        for p in picks:
            c = _corr(r.rets_1h, p.rets_1h)
            if not math.isnan(c) and c > 0.85:
                ok = False
                break
        if ok:
            picks.append(r)

    # ensure we have a majors anchor if possible
    print("\n=== Recommendation (2–3 contracts) ===")
    if not picks:
        print("Недостаточно кандидатов под фильтры — смотри таблицу вручную.")
    else:
        roles = ["ядро / beta", "альфа / средний corr", "диверсификатор / низкий corr"]
        for i, r in enumerate(picks):
            role = roles[i] if i < len(roles) else "запас"
            print(
                f"  {i+1}. {r.symbol} — {role}: "
                f"turnover ${r.turnover24h/1e6:.0f}M, min≈${fmt(r.min_usd, 2)}, "
                f"spread {fmt(r.spread_pct, 4)}%, ATR15 {fmt(r.atr15_pct)}%, "
                f"ER4h {fmt(r.er_4h)}, ρBTC {fmt(r.corr_btc, 2)}, "
                f"funding {fmt(r.funding*100, 4)}% / {fmt(r.funding_interval_h, 0)}h"
            )
        if len(picks) >= 2:
            print("\nПопарные corr выбранных:")
            for i in range(len(picks)):
                for j in range(i + 1, len(picks)):
                    c = _corr(picks[i].rets_1h, picks[j].rets_1h)
                    print(f"  {picks[i].symbol} vs {picks[j].symbol}: {fmt(c, 3)}")

    # CSV
    fieldnames = [
        "symbol",
        "turnover24h_m",
        "last",
        "min_qty",
        "qty_step",
        "min_notional",
        "min_usd",
        "spread_pct",
        "depth_10bps_usd",
        "depth_25bps_usd",
        "funding",
        "funding_interval_h",
        "atr15_pct",
        "atr1h_pct",
        "er_4h",
        "er_12h",
        "wick15",
        "corr_btc",
        "flags",
    ]
    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in ranked:
            w.writerow(
                {
                    "symbol": r.symbol,
                    "turnover24h_m": round(r.turnover24h / 1e6, 3),
                    "last": r.last,
                    "min_qty": r.min_qty,
                    "qty_step": r.qty_step,
                    "min_notional": r.min_notional,
                    "min_usd": None if math.isnan(r.min_usd) else round(r.min_usd, 4),
                    "spread_pct": None if math.isnan(r.spread_pct) else round(r.spread_pct, 5),
                    "depth_10bps_usd": None
                    if math.isnan(r.depth_10bps_usd)
                    else round(r.depth_10bps_usd, 2),
                    "depth_25bps_usd": None
                    if math.isnan(r.depth_25bps_usd)
                    else round(r.depth_25bps_usd, 2),
                    "funding": r.funding,
                    "funding_interval_h": r.funding_interval_h,
                    "atr15_pct": None if math.isnan(r.atr15_pct) else round(r.atr15_pct, 5),
                    "atr1h_pct": None if math.isnan(r.atr1h_pct) else round(r.atr1h_pct, 5),
                    "er_4h": None if math.isnan(r.er_4h) else round(r.er_4h, 5),
                    "er_12h": None if math.isnan(r.er_12h) else round(r.er_12h, 5),
                    "wick15": None if math.isnan(r.wick15) else round(r.wick15, 5),
                    "corr_btc": None if math.isnan(r.corr_btc) else round(r.corr_btc, 5),
                    "flags": ";".join(r.flags),
                }
            )
    print(f"\nCSV → {args.csv}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
