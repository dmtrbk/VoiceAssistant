# skills/crypto/journal.py
# Дневник сделок крипты + одна цифра lifetime_pnl_usd (перезаписывается).

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import common
from .common import _JOURNAL_HINTS, _format_usd

logger = logging.getLogger(__name__)

_KEEP = object()
_MSK = ZoneInfo("Europe/Moscow")


def _wants_journal(text: str) -> bool:
    return any(hint in text for hint in _JOURNAL_HINTS)


def _empty_payload() -> dict[str, Any]:
    return {"trades": [], "lifetime_pnl_usd": None}


def _read_payload(path: str | None = None) -> dict[str, Any]:
    path = path or common._TRADE_PATH
    data = _empty_payload()
    if not os.path.isfile(path):
        return data
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return data
    if isinstance(raw, list):
        data["trades"] = [item for item in raw if isinstance(item, dict)]
        return data
    if not isinstance(raw, dict):
        return data
    trades = raw.get("trades") or []
    if isinstance(trades, list):
        data["trades"] = [item for item in trades if isinstance(item, dict)]
    life = raw.get("lifetime_pnl_usd")
    if life is None:
        data["lifetime_pnl_usd"] = None
    else:
        try:
            data["lifetime_pnl_usd"] = float(life)
        except (TypeError, ValueError):
            data["lifetime_pnl_usd"] = None
    return data


def _read_trades(path: str | None = None) -> list[dict[str, Any]]:
    return list(_read_payload(path)["trades"])


def _write_trades(
    trades: list[dict[str, Any]],
    path: str | None = None,
    lifetime_pnl_usd: Any = _KEEP,
) -> None:
    path = path or common._TRADE_PATH
    existing = _read_payload(path)
    if lifetime_pnl_usd is _KEEP:
        life = existing.get("lifetime_pnl_usd")
    else:
        life = lifetime_pnl_usd
        if life is not None:
            try:
                life = round(float(life), 2)
            except (TypeError, ValueError):
                life = None
    keep = max(200, int(getattr(common, "_TRADE_HISTORY_KEEP", 5000) or 5000))
    payload = {"trades": trades[-keep:], "lifetime_pnl_usd": life}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] не записал дневник: %s", exc)


def read_lifetime_pnl(path: str | None = None) -> float | None:
    life = _read_payload(path).get("lifetime_pnl_usd")
    if life is None:
        return None
    try:
        return float(life)
    except (TypeError, ValueError):
        return None


def _entry_fee_usd(entry: dict[str, Any]) -> float:
    try:
        return max(0.0, float(entry.get("fee_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def execution_fee_usdt(row: dict[str, Any]) -> float:
    """Комиссия fill Bybit в USDT: sell обычно USDT, buy — в монете."""
    try:
        fee = float(row.get("execFee") or 0)
    except (TypeError, ValueError):
        return 0.0
    if fee <= 0:
        return 0.0
    try:
        px = float(row.get("execPrice") or 0)
    except (TypeError, ValueError):
        px = 0.0
    cur = str(row.get("feeCurrency") or "").upper()
    if cur in {"USDT", "USD"}:
        return fee
    side = str(row.get("side") or "").lower()
    if cur or side == "buy":
        return fee * px if px > 0 else 0.0
    return fee


def open_lots(
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, float]]:
    """Оставшиеся лоты: qty по журналу и сколько USDT в них ещё сидит."""
    rows = trades if trades is not None else _read_trades()
    lots: dict[str, dict[str, float]] = {}
    for entry in rows:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        quote = float(entry.get("quote") or 0)
        price = float(entry.get("price") or 0)
        if not ticker or price <= 0 or quote <= 0:
            continue
        qty = quote / price
        if qty <= 0:
            continue
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += quote + _entry_fee_usd(entry)
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        avg = lot["cost"] / lot["qty"]
        lot["qty"] -= sell_qty
        lot["cost"] -= avg * sell_qty
        if lot["qty"] < 1e-12 or lot["cost"] < 0.02:
            lot["qty"] = 0.0
            lot["cost"] = 0.0
    return {
        ticker: lot
        for ticker, lot in lots.items()
        if lot["qty"] > 0 and lot["cost"] > 0
    }


def open_avg_costs(
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Средняя цена оставшихся лотов по журналу (FIFO-cost)."""
    return {
        ticker: lot["cost"] / lot["qty"]
        for ticker, lot in open_lots(trades).items()
    }


def compute_lifetime_from_trades(
    trades: list[dict[str, Any]],
    mark_prices: dict[str, float] | None = None,
) -> float | None:
    """Реализованное по журналу + нереализованное по текущим ценам. Одна сумма USD."""
    if not trades:
        return None
    prices = {str(k).upper(): float(v) for k, v in (mark_prices or {}).items() if v}
    lots: dict[str, dict[str, float]] = {}
    realized = 0.0
    for entry in trades:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        quote = float(entry.get("quote") or 0)
        price = float(entry.get("price") or 0)
        if not ticker or price <= 0 or quote <= 0:
            continue
        qty = quote / price
        if qty <= 0:
            continue
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        fee = _entry_fee_usd(entry)
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += quote + fee
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        avg = lot["cost"] / lot["qty"]
        cost_sold = avg * sell_qty
        proceeds = quote * (sell_qty / qty)
        realized += proceeds - cost_sold - fee * (sell_qty / qty)
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-12 or lot["cost"] < 0.02:
            lot["qty"] = 0.0
            lot["cost"] = 0.0

    unrealized = 0.0
    for ticker, lot in lots.items():
        if lot["qty"] <= 0:
            continue
        px = prices.get(ticker)
        mark_value = lot["qty"] * px if px and px > 0 else lot["cost"]
        unrealized += mark_value - lot["cost"]
    return round(realized + unrealized, 2)


def compute_realized_from_trades(trades: list[dict[str, Any]]) -> float | None:
    """Только закрытые сделки: деньги уже на счёте, переоценка открытых не в счёт."""
    return compute_lifetime_from_trades(trades, {})


def recompute_lifetime_pnl(
    mark_prices: dict[str, float] | None = None,
    path: str | None = None,
) -> float | None:
    """Пересчитать и перезаписать lifetime_pnl_usd — зафиксированное по журналу."""
    trades = _read_trades(path)
    annotate_sell_pnl(trades)
    if mark_prices is None:
        value = compute_realized_from_trades(trades)
    else:
        value = compute_lifetime_from_trades(trades, mark_prices)
    _write_trades(trades, path=path, lifetime_pnl_usd=value)
    return value


def realized_pnl_for_sell(
    trades: list[dict[str, Any]],
    *,
    ticker: str,
    quote: float,
    price: float,
    fee_usd: float = 0.0,
) -> float | None:
    """Результат продажи против средней цены лота в журнале до этой сделки."""
    symbol = str(ticker or "").upper()
    quote_usd = float(quote or 0)
    px = float(price or 0)
    if not symbol or quote_usd <= 0 or px <= 0:
        return None
    lot = open_lots(trades).get(symbol)
    if not lot or lot["qty"] <= 0 or lot["cost"] <= 0:
        return None
    qty = quote_usd / px
    if qty <= 0:
        return None
    sell_qty = min(qty, lot["qty"])
    avg = lot["cost"] / lot["qty"]
    cost_sold = avg * sell_qty
    proceeds = quote_usd * (sell_qty / qty)
    fee = max(0.0, float(fee_usd or 0)) * (sell_qty / qty)
    return round(proceeds - cost_sold - fee, 2)


def annotate_sell_pnl(trades: list[dict[str, Any]]) -> None:
    """Проставить pnl_usd по каждой продаже с учётом комиссий."""
    seen: list[dict[str, Any]] = []
    for entry in trades:
        if str(entry.get("side") or "").lower() == "sell":
            pnl = realized_pnl_for_sell(
                seen,
                ticker=str(entry.get("ticker") or ""),
                quote=float(entry.get("quote") or 0),
                price=float(entry.get("price") or 0),
                fee_usd=_entry_fee_usd(entry),
            )
            if pnl is not None:
                entry["pnl_usd"] = pnl
            else:
                entry.pop("pnl_usd", None)
        seen.append(entry)


def cluster_executions(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сумма fill одной заявки Bybit: quote, qty, fee, время."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in fills:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        ticker = sym[:-4].upper()
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        try:
            qty = float(row.get("execQty") or 0)
            val = float(row.get("execValue") or 0)
            px = float(row.get("execPrice") or 0)
            ts_ms = int(row.get("execTime") or 0)
        except (TypeError, ValueError):
            continue
        if val <= 0 and qty > 0 and px > 0:
            val = qty * px
        if qty <= 0 or val <= 0:
            continue
        oid = str(row.get("orderId") or row.get("execId") or f"{ticker}:{side}:{ts_ms}")
        key = f"{ticker}:{side}:{oid}"
        item = groups.get(key)
        if item is None:
            item = {
                "ticker": ticker,
                "side": side,
                "order_id": oid,
                "qty": 0.0,
                "quote": 0.0,
                "fee": 0.0,
                "ts_ms": ts_ms,
            }
            groups[key] = item
            order.append(key)
        item["qty"] += qty
        item["quote"] += val
        item["fee"] += execution_fee_usdt(row)
        if ts_ms and (not item["ts_ms"] or ts_ms < item["ts_ms"]):
            item["ts_ms"] = ts_ms
    out: list[dict[str, Any]] = []
    for key in order:
        item = groups[key]
        if item["qty"] <= 0:
            continue
        item["price"] = item["quote"] / item["qty"]
        out.append(item)
    return out


def _match_fill_group(
    entry: dict[str, Any],
    groups: list[dict[str, Any]],
    used: set[int],
    *,
    window_ms: int = 45 * 60 * 1000,
) -> dict[str, Any] | None:
    ticker = str(entry.get("ticker") or "").upper()
    side = str(entry.get("side") or "").lower()
    try:
        ts_ms = int(float(entry.get("ts_epoch") or 0) * 1000)
        quote = float(entry.get("quote") or 0)
    except (TypeError, ValueError):
        return None
    if not ticker or side not in {"buy", "sell"} or ts_ms <= 0:
        return None
    best: tuple[float, float, dict[str, Any], int] | None = None
    for idx, group in enumerate(groups):
        if idx in used:
            continue
        if group["ticker"] != ticker or group["side"] != side:
            continue
        dt = abs(int(group["ts_ms"] or 0) - ts_ms)
        if dt > window_ms:
            continue
        dq = abs(float(group["quote"]) - quote)
        score = (dq, dt)
        if best is None or score < best[:2]:
            best = (dq, float(dt), group, idx)
    if best is None:
        return None
    used.add(best[3])
    return best[2]


def sync_journal_with_fills(
    trades: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    wallet_qty: dict[str, float],
    *,
    min_lot_usd: float = 5.0,
    dust_usd: float = 1.0,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Комиссии с биржи на строки журнала; проданные на Bybit лоты — в дневник."""
    rows = [dict(entry) for entry in trades]
    groups = cluster_executions(fills)
    used: set[int] = set()
    notes: list[str] = []
    changed = False
    for entry in rows:
        group = _match_fill_group(entry, groups, used)
        if group is None:
            if "fee_usd" not in entry:
                entry["fee_usd"] = 0.0
                changed = True
            continue
        fee = round(float(group["fee"]), 6)
        if abs(_entry_fee_usd(entry) - fee) > 1e-9 or "fee_usd" not in entry:
            entry["fee_usd"] = fee
            changed = True
    lots = open_lots(rows)
    for ticker, lot in list(lots.items()):
        if lot["cost"] + 1e-9 < min_lot_usd:
            continue
        held = float(wallet_qty.get(ticker) or 0)
        avg = lot["cost"] / lot["qty"] if lot["qty"] else 0.0
        if held * avg >= dust_usd:
            continue
        last_ms = 0
        name = ticker
        for entry in rows:
            if str(entry.get("ticker") or "").upper() != ticker:
                continue
            name = str(entry.get("name") or name)
            try:
                last_ms = max(last_ms, int(float(entry.get("ts_epoch") or 0) * 1000))
            except (TypeError, ValueError):
                pass
        remain = lot["qty"]
        added = 0
        for idx, group in enumerate(groups):
            if remain * avg < dust_usd:
                break
            if idx in used or group["ticker"] != ticker or group["side"] != "sell":
                continue
            if int(group["ts_ms"] or 0) + 60_000 < last_ms:
                continue
            quote = round(float(group["quote"]), 2)
            if quote <= 0 or remain <= 0:
                continue
            used.add(idx)
            ts_ms = int(group["ts_ms"] or last_ms)
            dt = datetime.fromtimestamp(ts_ms / 1000.0, _MSK)
            rows.append(
                {
                    "ts": dt.strftime("%d.%m %H:%M"),
                    "ts_epoch": int(ts_ms / 1000),
                    "ticker": ticker,
                    "name": name,
                    "side": "sell",
                    "quote": quote,
                    "price": quote / remain,
                    "fee_usd": round(float(group["fee"]), 6),
                }
            )
            remain = 0.0
            added += 1
            changed = True
            break
        if added:
            notes.append(f"{ticker} закрыл по бирже")
    if changed:
        rows.sort(key=lambda item: float(item.get("ts_epoch") or 0))
        annotate_sell_pnl(rows)
    return rows, notes, changed


def format_trade_line(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "").strip()
    side = "купил" if str(entry.get("side") or "") == "buy" else "продал"
    name = str(entry.get("name") or entry.get("ticker") or "")
    ticker = str(entry.get("ticker") or "")
    quote = entry.get("quote") or 0
    price = entry.get("price") or 0
    quote_bit = f" на {_format_usd(float(quote))}" if quote else ""
    price_bit = f" по {price:g}" if price else ""
    pnl_bit = ""
    if side == "продал" and entry.get("pnl_usd") is not None:
        try:
            pnl_bit = f" ({float(entry['pnl_usd']):+.2f}$)"
        except (TypeError, ValueError):
            pnl_bit = ""
    when = f"{ts} " if ts else ""
    return f"{when}{side} {name} ({ticker}){quote_bit}{price_bit}{pnl_bit}"


def format_journal(trades: list[dict[str, Any]] | None = None, limit: int = 8) -> str:
    rows = trades if trades is not None else _read_trades()
    if not rows:
        return ""
    lines = [format_trade_line(item) for item in rows[-limit:]]
    return "Дневник крипты:\n" + "\n".join(lines)


class CryptoJournalMixin:
    def _run_journal(self, channel: str = "voice") -> str:
        text = format_journal() or "Дневник пуст: сделок ещё не было."
        if channel == "telegram":
            return text
        if common.telegram_configured():
            common.send_telegram_notification(text, background=False)
            return "Отправил дневник крипты в телеграм."
        return text

    def _journal_trade(
        self,
        ticker: str,
        side: str,
        quote: float,
        price: float,
        spoken: str,
        fee_usd: float = 0.0,
    ) -> None:
        now = datetime.now(_MSK)
        side_norm = "buy" if side == "Buy" else "sell"
        quote_usd = round(float(quote or 0), 2)
        px = round(float(price or 0), 6)
        fee = round(max(0.0, float(fee_usd or 0)), 6)
        trades = _read_trades()
        entry: dict[str, Any] = {
            "ts": now.strftime("%d.%m %H:%M"),
            "ts_epoch": int(now.timestamp()),
            "ticker": ticker,
            "name": spoken,
            "side": side_norm,
            "quote": quote_usd,
            "price": px,
            "fee_usd": fee,
        }
        if side_norm == "sell":
            pnl = realized_pnl_for_sell(
                trades, ticker=ticker, quote=quote_usd, price=px, fee_usd=fee
            )
            if pnl is not None:
                entry["pnl_usd"] = pnl
        trades.append(entry)
        _write_trades(trades)
        try:
            recompute_lifetime_pnl()
        except Exception as exc:
            logger.warning("[Крипта] lifetime после сделки: %s", exc)
        try:
            import conky_markets
            conky_markets.poke_refresh()
        except Exception:
            pass
        line = "Дневник крипты: " + format_trade_line(entry)
        logger.info("[Крипта] %s", line)
        if common.telegram_configured():
            common.send_telegram_notification(line, background=True)

    def _spot_fill_history(
        self,
        start_ms: int,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        end_ms = int(time.time() * 1000)
        for _ in range(80):
            params: dict[str, Any] = {
                "category": "spot",
                "limit": "100",
                "startTime": str(int(start_ms)),
                "endTime": str(end_ms),
            }
            if symbol:
                params["symbol"] = symbol
            if cursor:
                params["cursor"] = cursor
            data = self._signed("GET", "/v5/execution/list", params)
            result = data.get("result") or {}
            chunk = result.get("list") or []
            rows.extend(row for row in chunk if isinstance(row, dict))
            cursor = str(result.get("nextPageCursor") or "")
            if not chunk or not cursor:
                break
        return rows

    def _journal_sync_exchange(
        self,
        wallet_qty: dict[str, float],
        *,
        min_lot_usd: float = 5.0,
    ) -> list[str]:
        """Сверить дневник с fill Bybit: комиссии и продажи, которых нет в журнале."""
        trades = _read_trades()
        if not trades:
            return []
        lots = open_lots(trades)
        ghosts = [
            ticker
            for ticker, lot in lots.items()
            if lot["cost"] >= min_lot_usd
            and float(wallet_qty.get(ticker) or 0) * (lot["cost"] / lot["qty"]) < 1.0
        ]
        need_fees = any("fee_usd" not in row for row in trades)
        if not need_fees and not ghosts:
            return []
        start_ms = min(int(float(row.get("ts_epoch") or 0) * 1000) for row in trades)
        start_ms = max(0, start_ms - 3600_000)
        fills: list[dict[str, Any]] = []
        if need_fees:
            fills = self._spot_fill_history(start_ms)
        else:
            for ticker in ghosts:
                fills.extend(self._spot_fill_history(start_ms, symbol=f"{ticker}USDT"))
        updated, notes, changed = sync_journal_with_fills(
            trades,
            fills,
            wallet_qty,
            min_lot_usd=min_lot_usd,
        )
        if not changed:
            return notes
        _write_trades(updated)
        try:
            recompute_lifetime_pnl()
        except Exception as exc:
            logger.warning("[Крипта] lifetime после сверки: %s", exc)
        try:
            import conky_markets
            conky_markets.poke_refresh()
        except Exception:
            pass
        return notes


def _entry_epoch(entry: dict[str, Any]) -> float | None:
    raw = entry.get("ts_epoch")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    ts = str(entry.get("ts") or "").strip()
    if not ts:
        return None
    # Старый формат «дд.мм ЧЧ:ММ» — считаем текущий год МСК.
    try:
        now = datetime.now(_MSK)
        parsed = datetime.strptime(f"{ts} {now.year}", "%d.%m %H:%M %Y").replace(tzinfo=_MSK)
        if parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed.timestamp()
    except ValueError:
        return None


def cooldown_tickers(
    hours: float = 12.0,
    trades: list[dict[str, Any]] | None = None,
    *,
    now: float | None = None,
) -> set[str]:
    """Тикеры с убыточной продажей за последние hours — не докупать автостолом."""
    rows = trades if trades is not None else _read_trades()
    cutoff = (now if now is not None else time.time()) - max(0.0, hours) * 3600.0
    lots: dict[str, dict[str, float]] = {}
    cool: set[str] = set()
    for entry in rows:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        quote = float(entry.get("quote") or 0)
        price = float(entry.get("price") or 0)
        if not ticker or price <= 0 or quote <= 0:
            continue
        qty = quote / price
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += quote
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        avg = lot["cost"] / lot["qty"]
        cost_sold = avg * sell_qty
        proceeds = quote * (sell_qty / qty)
        epoch = _entry_epoch(entry)
        if epoch is not None and epoch >= cutoff and proceeds < cost_sold:
            cool.add(ticker)
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-12:
            lot["qty"] = 0.0
            lot["cost"] = 0.0
    return cool


def recently_bought(
    hours: float = 12.0,
    trades: list[dict[str, Any]] | None = None,
    *,
    now: float | None = None,
) -> set[str]:
    rows = trades if trades is not None else _read_trades()
    cutoff = (now if now is not None else time.time()) - max(0.0, hours) * 3600.0
    out: set[str] = set()
    for entry in rows:
        if str(entry.get("side") or "").lower() != "buy":
            continue
        epoch = _entry_epoch(entry)
        if epoch is None or epoch < cutoff:
            continue
        ticker = str(entry.get("ticker") or "").upper()
        if ticker:
            out.add(ticker)
    return out


def recently_sold(
    hours: float = 2.0,
    trades: list[dict[str, Any]] | None = None,
    *,
    now: float | None = None,
) -> set[str]:
    """Любая продажа за окно — щуп не сразу обратно в ту же монету (даже в плюс)."""
    rows = trades if trades is not None else _read_trades()
    cutoff = (now if now is not None else time.time()) - max(0.0, hours) * 3600.0
    out: set[str] = set()
    for entry in rows:
        if str(entry.get("side") or "").lower() != "sell":
            continue
        epoch = _entry_epoch(entry)
        if epoch is None or epoch < cutoff:
            continue
        ticker = str(entry.get("ticker") or "").upper()
        if ticker:
            out.add(ticker)
    return out


def last_sell_price(
    ticker: str,
    trades: list[dict[str, Any]] | None = None,
) -> float | None:
    """Цена последней продажи тикера (для breakout-reentry)."""
    name = str(ticker or "").upper()
    if not name:
        return None
    rows = trades if trades is not None else _read_trades()
    best_epoch = -1.0
    price: float | None = None
    for entry in rows:
        if str(entry.get("side") or "").lower() != "sell":
            continue
        if str(entry.get("ticker") or "").upper() != name:
            continue
        epoch = _entry_epoch(entry)
        if epoch is None:
            continue
        px = float(entry.get("price") or 0)
        if px <= 0:
            continue
        if epoch >= best_epoch:
            best_epoch = epoch
            price = px
    return price


def trades_on_msk_day(
    day: datetime | None = None,
    trades: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    day = day or datetime.now(_MSK)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    rows = trades if trades is not None else _read_trades()
    out: list[dict[str, Any]] = []
    for entry in rows:
        epoch = _entry_epoch(entry)
        if epoch is None:
            continue
        if start.timestamp() <= epoch < end.timestamp():
            out.append(entry)
    return out


def daily_realized_pnl(
    day: datetime | None = None,
    trades: list[dict[str, Any]] | None = None,
) -> float:
    """Зафиксированный PnL по закрытым продажам за календарный день МСК (FIFO-cost)."""
    day = day or datetime.now(_MSK)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = (day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    rows = trades if trades is not None else _read_trades()
    lots: dict[str, dict[str, float]] = {}
    day_pnl = 0.0
    for entry in rows:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        quote = float(entry.get("quote") or 0)
        price = float(entry.get("price") or 0)
        if not ticker or price <= 0 or quote <= 0:
            continue
        qty = quote / price
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += quote
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        avg = lot["cost"] / lot["qty"]
        cost_sold = avg * sell_qty
        proceeds = quote * (sell_qty / qty)
        epoch = _entry_epoch(entry)
        if epoch is not None and start <= epoch < end:
            day_pnl += proceeds - cost_sold
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-12:
            lot["qty"] = 0.0
            lot["cost"] = 0.0
    return round(day_pnl, 2)


def probe_daily_loss_reset_ts(path: str | None = None) -> float:
    """Метка сброса дневного стопа −$6. 0 — считать с начала календарных суток МСК."""
    try:
        return float(_read_daily_meta(path).get("probe_loss_reset_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def reset_probe_daily_loss(
    *,
    now: float | None = None,
    path: str | None = None,
) -> float:
    """Обнулить счётчик дневного стопа: новые входы смотрят PnL только после этой метки."""
    ts = float(now if now is not None else time.time())
    meta = _read_daily_meta(path)
    meta["probe_loss_reset_ts"] = ts
    _write_daily_meta(meta, path)
    logger.info("[Крипта] дневной стоп −$6 сброшен, считаю с %s", datetime.fromtimestamp(ts, _MSK).strftime("%d.%m %H:%M"))
    return ts


def probe_loss_guard_reason(
    *,
    now: float | None = None,
    trades: list[dict[str, Any]] | None = None,
    reset_ts: float | None = None,
    enabled: bool | None = None,
) -> str | None:
    """Жёсткий режим входов (не стоп скана): дневной лимит, час, серия убытков или полка."""
    from .desk_policy import (
        PROBE_HOURLY_MAX_LOSS_USD,
        PROBE_MAX_CONSECUTIVE_LOSSES,
        PROBE_SHELF_COOLDOWN_LOSS_USD,
        PROBE_SHELF_COOLDOWN_SEC,
        PROBE_STREAK_PAUSE_SEC,
        PROBE_STRICT_MODE,
        probe_daily_loss_blocked,
    )

    if enabled is None:
        enabled = bool(PROBE_STRICT_MODE)
    if not enabled:
        return None

    now_ts = float(now if now is not None else time.time())
    if reset_ts is None:
        reset_ts = 0.0 if trades is not None else probe_daily_loss_reset_ts()
    day_dt = datetime.fromtimestamp(now_ts, _MSK)
    day_start = max(
        day_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
        float(reset_ts or 0),
    )
    day_end = (day_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    rows = trades if trades is not None else _read_trades()
    lots: dict[str, dict[str, float]] = {}
    day_pnl = 0.0
    closed: list[tuple[float, float]] = []
    for entry in rows:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        quote = float(entry.get("quote") or 0)
        price = float(entry.get("price") or 0)
        if not ticker or price <= 0 or quote <= 0:
            continue
        qty = quote / price
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += quote
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        avg = lot["cost"] / lot["qty"]
        cost_sold = avg * sell_qty
        proceeds = quote * (sell_qty / qty)
        pnl = proceeds - cost_sold
        epoch = _entry_epoch(entry)
        if epoch is not None and epoch <= now_ts + 1.0:
            if day_start <= epoch < day_end:
                day_pnl += pnl
            closed.append((epoch, pnl))
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-12:
            lot["qty"] = 0.0
            lot["cost"] = 0.0
    if probe_daily_loss_blocked(round(day_pnl, 2)):
        return f"дневной лимит убытка ({day_pnl:+.2f}$)"
    hour_pnl = sum(pnl for ep, pnl in closed if now_ts - 3600.0 <= ep <= now_ts)
    if hour_pnl <= -PROBE_HOURLY_MAX_LOSS_USD + 1e-9:
        return f"просадка за час ({hour_pnl:+.2f}$)"
    if len(closed) >= PROBE_MAX_CONSECUTIVE_LOSSES:
        tail = closed[-PROBE_MAX_CONSECUTIVE_LOSSES:]
        if all(pnl < -0.15 for _ep, pnl in tail):
            last_ep = tail[-1][0]
            if now_ts - last_ep < PROBE_STREAK_PAUSE_SEC:
                return f"серия из {PROBE_MAX_CONSECUTIVE_LOSSES} убытков (пауза 60м)"
    shelf_hits = [
        ep
        for ep, pnl in closed
        if now_ts - PROBE_SHELF_COOLDOWN_SEC <= ep <= now_ts + 1.0
        and pnl <= -PROBE_SHELF_COOLDOWN_LOSS_USD + 1e-9
    ]
    if shelf_hits:
        last_ep = max(shelf_hits)
        left = PROBE_SHELF_COOLDOWN_SEC - (now_ts - last_ep)
        mins = max(1, int((left + 59) // 60))
        return f"после полки пауза {mins}м"
    return None


def format_daily_pnl_report(
    trades: list[dict[str, Any]] | None = None,
    lifetime: float | None = None,
    *,
    day: datetime | None = None,
) -> str:
    day = day or datetime.now(_MSK)
    day_trades = trades_on_msk_day(day, trades)
    buys = sum(1 for t in day_trades if str(t.get("side")) == "buy")
    sells = sum(1 for t in day_trades if str(t.get("side")) == "sell")
    volume = sum(float(t.get("quote") or 0) for t in day_trades)
    life = lifetime if lifetime is not None else read_lifetime_pnl()
    lines = [
        f"Крипта за {day.strftime('%d.%m')}: сделок {len(day_trades)} "
        f"(покупок {buys}, продаж {sells}), оборот {_format_usd(volume)}.",
    ]
    if life is not None:
        lines.append(f"Lifetime: {_format_usd(life, signed=True)}.")
    if day_trades:
        lines.append("Последние:")
        lines.extend(format_trade_line(item) for item in day_trades[-5:])
    else:
        lines.append("Сегодня сделок не было.")
    return "\n".join(lines)


def _read_daily_meta(path: str | None = None) -> dict[str, Any]:
    path = path or common._DAILY_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_daily_meta(payload: dict[str, Any], path: str | None = None) -> None:
    path = path or common._DAILY_PATH
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] не записал daily meta: %s", exc)


def should_send_daily_report(*, now: datetime | None = None, path: str | None = None) -> bool:
    """Один отчёт в календарный день МСК после 21:00."""
    now = now or datetime.now(_MSK)
    if now.hour < 21:
        return False
    key = now.strftime("%Y-%m-%d")
    meta = _read_daily_meta(path)
    return meta.get("last_report_day") != key


def mark_daily_report_sent(*, now: datetime | None = None, path: str | None = None) -> None:
    now = now or datetime.now(_MSK)
    meta = _read_daily_meta(path)
    meta["last_report_day"] = now.strftime("%Y-%m-%d")
    _write_daily_meta(meta, path)


_PAPER_EVENTS_KEEP = 500
_PAPER_RELOG_SEC = 15 * 60


def _empty_probe_paper() -> dict[str, Any]:
    return {"open": [], "events": []}


def read_probe_paper(path: str | None = None) -> dict[str, Any]:
    path = path or common._PROBE_PAPER_PATH
    empty = _empty_probe_paper()
    if not os.path.isfile(path):
        return empty
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return empty
    if not isinstance(raw, dict):
        return empty
    open_legs = raw.get("open") if isinstance(raw.get("open"), list) else []
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    return {
        "open": [item for item in open_legs if isinstance(item, dict)],
        "events": [item for item in events if isinstance(item, dict)],
    }


def _write_probe_paper(payload: dict[str, Any], path: str | None = None) -> None:
    path = path or common._PROBE_PAPER_PATH
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] не записал имитацию щупа: %s", exc)


def probe_paper_open_legs(path: str | None = None) -> list[dict[str, Any]]:
    return list(read_probe_paper(path).get("open") or [])


def probe_paper_recent(
    ticker: str,
    kind: str,
    *,
    within_sec: float = _PAPER_RELOG_SEC,
    now: float | None = None,
    path: str | None = None,
) -> bool:
    name = str(ticker or "").upper()
    want = str(kind or "")
    if not name or not want:
        return False
    now_ts = float(now if now is not None else time.time())
    for event in reversed(read_probe_paper(path).get("events") or []):
        if str(event.get("ticker") or "").upper() != name:
            continue
        if str(event.get("kind") or "") != want:
            continue
        ts = float(event.get("ts") or 0)
        return now_ts - ts < float(within_sec)
    return False


def record_probe_paper(
    *,
    kind: str,
    ticker: str,
    algo: str = "",
    shelf: float = 0.0,
    price: float = 0.0,
    quote: float = 0.0,
    reason: str = "",
    extra: dict[str, Any] | None = None,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Журнал имитации: сетап, виртуальная покупка, виртуальный выход. Ордеров нет."""
    payload = read_probe_paper(path)
    name = str(ticker or "").upper()
    ts = float(now if now is not None else time.time())
    event: dict[str, Any] = {
        "ts": ts,
        "kind": str(kind or ""),
        "ticker": name,
        "algo": str(algo or ""),
        "shelf": float(shelf or 0),
        "price": float(price or 0),
        "quote": float(quote or 0),
        "reason": str(reason or ""),
    }
    if extra:
        for key, value in extra.items():
            if key in event or value is None:
                continue
            event[key] = value
    events = list(payload.get("events") or [])
    events.append(event)
    payload["events"] = events[-_PAPER_EVENTS_KEEP:]
    open_legs = [leg for leg in payload.get("open") or [] if isinstance(leg, dict)]
    if kind == "buy" and name and float(price or 0) > 0:
        open_legs = [leg for leg in open_legs if str(leg.get("ticker") or "").upper() != name]
        qty = float(quote or 0) / float(price) if float(price) > 0 else 0.0
        open_legs.append({
            "ticker": name,
            "qty": qty,
            "entry": float(price),
            "shelf": float(shelf or 0),
            "arm_px": float((extra or {}).get("arm_px") or price),
            "peak": float(price),
            "low": float((extra or {}).get("low") or 0),
            "ts": ts,
            "algo": str(algo or ""),
        })
    if kind in {"exit", "drop"} and name:
        open_legs = [leg for leg in open_legs if str(leg.get("ticker") or "").upper() != name]
    payload["open"] = open_legs
    _write_probe_paper(payload, path)
    return event


def update_probe_paper_leg(
    ticker: str,
    *,
    peak: float | None = None,
    path: str | None = None,
) -> None:
    name = str(ticker or "").upper()
    if not name:
        return
    payload = read_probe_paper(path)
    changed = False
    for leg in payload.get("open") or []:
        if str(leg.get("ticker") or "").upper() != name:
            continue
        if peak is not None and float(peak) > float(leg.get("peak") or 0) + 1e-12:
            leg["peak"] = float(peak)
            changed = True
        break
    if changed:
        _write_probe_paper(payload, path)
