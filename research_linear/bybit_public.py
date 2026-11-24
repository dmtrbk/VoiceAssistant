"""Публичный REST Bybit v5 для research (без ключей)."""

from __future__ import annotations

import time
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
# Акции / сырьё / индексы / ETF-прокси на linear — не крипта
NON_CRYPTO_BASES = frozenset(
    {
        "SOXL",
        "SOXS",
        "NVDA",
        "TSLA",
        "AAPL",
        "AMZN",
        "META",
        "MSFT",
        "GOOG",
        "GOOGL",
        "COIN",
        "MSTR",
        "SNDK",
        "SKHYNIX",
        "SKHY",
        "MU",
        "QQQ",
        "SPX",
        "SPY",
        "SPCX",
        "SNXX",
        "KORU",
        "XAU",
        "XAG",
        "XAUT",  # tokenized gold
        "CL",
        "ANT",  # Antares / equity-like on board
        "TQQQ",
        "SQQQ",
        "HOOD",
        "PLTR",
        "CRCL",
        "INTC",
        "AMD",
        "BABA",
        "NFLX",
    }
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "VoiceAssistant-linear-research/2.0"})


def get(path: str, params: dict[str, Any] | None = None, *, retries: int = 5) -> dict[str, Any]:
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            r = SESSION.get(f"{HOST}{path}", params=params or {}, timeout=40)
            if r.status_code in {429, 502, 503, 504}:
                time.sleep(0.7 * (2**attempt))
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


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def is_crypto_perp_base(base: str) -> bool:
    b = (base or "").upper()
    if not b or b in STABLE_BASES or b in NON_CRYPTO_BASES:
        return False
    # токенизированные акции часто без суффикса; эвристика по уже известному списку
    return True


def fetch_instruments_linear() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = get("/v5/market/instruments-info", params)
        result = data.get("result") or {}
        for row in result.get("list") or []:
            if str(row.get("status") or "") != "Trading":
                continue
            if str(row.get("quoteCoin") or "").upper() != "USDT":
                continue
            ctype = str(row.get("contractType") or "")
            if ctype and "Perpetual" not in ctype:
                continue
            base = str(row.get("baseCoin") or "").upper()
            symbol = str(row.get("symbol") or "")
            if not symbol.endswith("USDT") or not is_crypto_perp_base(base):
                continue
            # 1000PEPE etc — base often PEPE
            lot = row.get("lotSizeFilter") or {}
            fi_min = fnum(row.get("fundingInterval"), 480.0)
            out[symbol] = {
                "symbol": symbol,
                "base": base,
                "min_qty": fnum(lot.get("minOrderQty")),
                "qty_step": fnum(lot.get("qtyStep")),
                "min_notional": fnum(lot.get("minNotionalValue")),
                "funding_interval_h": fi_min / 60.0 if fi_min else 8.0,
            }
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
        time.sleep(0.04)
    return out


def fetch_tickers_linear() -> dict[str, dict[str, Any]]:
    data = get("/v5/market/tickers", {"category": "linear"})
    return {str(r.get("symbol")): r for r in (data.get("result") or {}).get("list") or [] if r.get("symbol")}


def fetch_orderbook(symbol: str, limit: int = 25) -> dict[str, Any]:
    data = get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": limit})
    return data.get("result") or {}


def orderbook_spread_depth(symbol: str) -> tuple[float, float]:
    """spread_pct, depth within ±0.10% of mid (USD)."""
    result = fetch_orderbook(symbol, 25)
    bids = [(fnum(p), fnum(q)) for p, q in (result.get("b") or [])]
    asks = [(fnum(p), fnum(q)) for p, q in (result.get("a") or [])]
    if not bids or not asks or bids[0][0] <= 0:
        return float("nan"), float("nan")
    best_bid, best_ask = bids[0][0], asks[0][0]
    spread_pct = (best_ask - best_bid) / best_bid * 100.0
    mid = (best_bid + best_ask) / 2.0
    band = mid * 0.001  # ±0.10%

    def side_usd(levels: list[tuple[float, float]], *, bid: bool) -> float:
        total = 0.0
        for px, qty in levels:
            if bid and px < mid - band:
                break
            if not bid and px > mid + band:
                break
            total += px * qty
        return total

    depth = side_usd(bids, bid=True) + side_usd(asks, bid=False)
    return spread_pct, depth


def fetch_klines(symbol: str, interval: str, days: int) -> list[list[Any]]:
    """Свечи oldest→newest. interval: '15'|'60'|'240'."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    all_rows: list[list[Any]] = []
    cursor_end = end_ms
    per_day = {"15": 96, "60": 24, "240": 6}.get(interval, 24)
    soft_cap = days * per_day + 100
    while cursor_end > start_ms and len(all_rows) < soft_cap:
        data = get(
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
        all_rows.extend(batch)
        oldest_ts = int(batch[-1][0])
        if oldest_ts <= start_ms:
            break
        cursor_end = oldest_ts - 1
        time.sleep(0.05)
    by_ts = {int(r[0]): r for r in all_rows}
    rows = [by_ts[k] for k in sorted(by_ts) if k >= start_ms]
    return rows


def fetch_funding_history(symbol: str, days: int) -> list[dict[str, Any]]:
    """Funding history oldest→newest. Bybit limit 200/page."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    rows: list[dict[str, Any]] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        data = get(
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": start_ms,
                "endTime": cursor_end,
                "limit": 200,
            },
        )
        batch = data.get("result", {}).get("list") or []
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(x.get("fundingRateTimestamp") or 0) for x in batch)
        if oldest <= start_ms:
            break
        cursor_end = oldest - 1
        time.sleep(0.05)
        if len(batch) < 200:
            break
    by_ts = {int(r["fundingRateTimestamp"]): r for r in rows if r.get("fundingRateTimestamp")}
    return [by_ts[k] for k in sorted(by_ts)]
