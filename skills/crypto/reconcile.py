# skills/crypto/reconcile.py
# Сверка локального состояния кармана/рукава с биржей (как sync open orders у Freqtrade).
# Щуп не трогаем: ни JSON, ни ноги щупа.

from __future__ import annotations

from typing import Any


# Зависший PostOnly после краша цикла: дольше maker-wait × 3.
ORPHAN_ORDER_AGE_SEC = 90.0
# Пыль на балансе не считаем живой ногой кармана.
POCKET_DUST_USD = 3.0


def wallet_qty_map(positions: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in positions or []:
        ticker = str(item.get("ticker") or "").upper()
        if not ticker:
            continue
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[ticker] = qty
    return out


def wallet_value_map(positions: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in positions or []:
        ticker = str(item.get("ticker") or "").upper()
        if not ticker:
            continue
        try:
            value = float(item.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[ticker] = value
    return out


def reconcile_pocket_qty(
    *,
    local_qty: float,
    wallet_qty: float,
    price: float,
    dust_usd: float = POCKET_DUST_USD,
) -> float:
    """Сколько qty оставить в файле кармана. 0 — карман закрыть.

    Биржа — источник истины: локальный хвост после ручной продажи / частичного fill обрезаем.
    """
    local = max(0.0, float(local_qty or 0))
    held = max(0.0, float(wallet_qty or 0))
    px = max(0.0, float(price or 0))
    if local <= 0:
        return 0.0
    if held <= 0 or (px > 0 and held * px < float(dust_usd)):
        return 0.0
    return min(local, held)


def reconcile_sleeve_alloc(
    alloc: dict[str, float],
    *,
    held_values: dict[str, float],
    probe_tickers: set[str],
    desk_bought: set[str],
    min_held_usd: float,
) -> dict[str, float]:
    """Убрать из цели рукава тикеры, которых уже нет на балансе (щуп не трогаем)."""
    probe = {str(t).upper() for t in probe_tickers}
    bought = {str(t).upper() for t in desk_bought}
    kept: dict[str, float] = {}
    for raw, pct in (alloc or {}).items():
        ticker = str(raw or "").upper()
        if not ticker:
            continue
        if ticker in probe:
            # Нога может совпадать с щупом — alloc рукава не чистим по щупу.
            kept[ticker] = float(pct)
            continue
        value = float(held_values.get(ticker) or 0)
        if ticker in bought and value < float(min_held_usd):
            continue
        kept[ticker] = float(pct)
    return kept


def reconcile_sleeve_maps(
    arms: dict[str, dict[str, float]],
    lows: dict[str, float],
    *,
    held_values: dict[str, float],
    probe_tickers: set[str],
    min_held_usd: float,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Сбросить exit/low рукава для монет, которых нет в портфеле (кроме щупа)."""
    probe = {str(t).upper() for t in probe_tickers}
    clean_arms: dict[str, dict[str, float]] = {}
    for raw, state in (arms or {}).items():
        ticker = str(raw or "").upper()
        if ticker in probe:
            clean_arms[ticker] = dict(state)
            continue
        if float(held_values.get(ticker) or 0) >= float(min_held_usd):
            clean_arms[ticker] = dict(state)
    clean_lows: dict[str, float] = {}
    for raw, low in (lows or {}).items():
        ticker = str(raw or "").upper()
        if ticker in probe:
            clean_lows[ticker] = float(low)
            continue
        if float(held_values.get(ticker) or 0) >= float(min_held_usd):
            clean_lows[ticker] = float(low)
    return clean_arms, clean_lows


def orphan_open_orders(
    rows: list[dict[str, Any]],
    *,
    now: float,
    skip_tickers: set[str],
    max_age_sec: float = ORPHAN_ORDER_AGE_SEC,
) -> list[dict[str, str]]:
    """Открытые лимитки старше max_age — кандидаты на отмену. Тикеры щупа пропускаем."""
    skip = {str(t).upper() for t in skip_tickers}
    out: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("orderStatus") or "")
        if status not in {"New", "PartiallyFilled"}:
            continue
        order_type = str(row.get("orderType") or "")
        if order_type and order_type.lower() != "limit":
            continue
        symbol = str(row.get("symbol") or "").upper()
        ticker = symbol[:-4] if symbol.endswith("USDT") else symbol
        if not ticker or ticker in skip:
            continue
        order_id = str(row.get("orderId") or "")
        if not order_id:
            continue
        try:
            created_raw = float(row.get("createdTime") or row.get("updatedTime") or 0)
        except (TypeError, ValueError):
            created_raw = 0.0
        # Bybit отдаёт ms; порог 1e11 ≈ 1973г в миллисекундах.
        created = created_raw / 1000.0 if created_raw > 1e11 else created_raw
        if created <= 0 or (now - created) < float(max_age_sec):
            continue
        out.append({"ticker": ticker, "order_id": order_id})
    return out
