"""REST warm-up истории kline с пагинацией (отсечение незакрытой свечи)."""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from .bybit_rest import BybitRest
from .config import LinearConfig

logger = logging.getLogger(__name__)

_INTERVAL_MS = {"15": 15 * 60_000, "60": 60 * 60_000, "240": 4 * 60 * 60_000}


def _rows_to_df(rows: list[list[Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    for c in ("open", "high", "low", "close", "volume", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = df["ts"].astype("int64")
    return df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def fetch_klines_paginated(
    rest: BybitRest,
    symbol: str,
    interval: str,
    want_bars: int,
    *,
    drop_unclosed: bool = True,
) -> pd.DataFrame:
    """Пагинация по 1000, newest→oldest batch, итог asc. Отсекает незакрытый бар."""
    end_ms = int(time.time() * 1000)
    period = _INTERVAL_MS[interval]
    all_rows: list[list[Any]] = []
    cursor_end = end_ms
    while len(all_rows) < want_bars + 5:
        data = rest.public_get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "end": cursor_end,
                "limit": 1000,
            },
        )
        batch = data.get("result", {}).get("list") or []
        if not batch:
            break
        all_rows.extend(batch)
        oldest = int(batch[-1][0])
        cursor_end = oldest - 1
        # throttle в BybitRest; доп. пауза между страницами warm
        time.sleep(0.12)
        if len(batch) < 1000:
            break
    by_ts = {int(r[0]): r for r in all_rows}
    rows = [by_ts[k] for k in sorted(by_ts)]
    if drop_unclosed and rows:
        # бар закрыт, если open_ts + period <= now
        now = int(time.time() * 1000)
        rows = [r for r in rows if int(r[0]) + period <= now]
    if len(rows) > want_bars:
        rows = rows[-want_bars:]
    df = _rows_to_df(rows)
    logger.info("[linear] warm %s %s → %d bars", symbol, interval, len(df))
    return df


def warm_symbol(rest: BybitRest, cfg: LinearConfig, symbol: str) -> dict[str, pd.DataFrame]:
    return {
        "15m": fetch_klines_paginated(rest, symbol, "15", cfg.warm_bars_15m),
        "1h": fetch_klines_paginated(rest, symbol, "60", cfg.warm_bars_1h),
        "4h": fetch_klines_paginated(rest, symbol, "240", cfg.warm_bars_4h),
    }


def warm_universe(rest: BybitRest, cfg: LinearConfig) -> dict[str, dict[str, pd.DataFrame]]:
    syms = set(cfg.symbols) | {cfg.btc_symbol}
    if cfg.shadow_enabled:
        syms |= set(cfg.shadow_symbols)
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for i, sym in enumerate(sorted(syms)):
        if i:
            time.sleep(0.4)  # перед следующим символом — дать остыть IP-лимиту
        out[sym] = warm_symbol(rest, cfg, sym)
    return out
