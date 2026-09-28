# skills/crypto/indicators.py
# Полосы Боллинджера по ценам закрытия. Без сети.

from __future__ import annotations

import math
from typing import Any, Sequence

BB_PERIOD = 20
BB_K = 2.0


def bollinger(
    closes: Sequence[float],
    *,
    period: int = BB_PERIOD,
    k: float = BB_K,
) -> dict[str, float] | None:
    """closes — от старых к новым; последняя = текущая цена. None — мало данных."""
    window = [float(c) for c in closes[-period:] if c is not None and float(c) > 0]
    if len(window) < period:
        return None
    ma = sum(window) / period
    std = math.sqrt(sum((c - ma) ** 2 for c in window) / period)
    price = window[-1]
    z = (price - ma) / std if std > 0 else 0.0
    return {
        "price": price,
        "ma": ma,
        "std": std,
        "upper": ma + k * std,
        "lower": ma - k * std,
        "z": z,
    }


def closes_from_kline(rows: Sequence[Any]) -> list[float]:
    """Bybit kline (новые первыми) → закрытия от старых к новым."""
    out: list[float] = []
    for item in rows:
        if isinstance(item, (list, tuple)) and len(item) > 4:
            try:
                out.append(float(item[4]))
            except (TypeError, ValueError):
                continue
    out.reverse()
    return out
