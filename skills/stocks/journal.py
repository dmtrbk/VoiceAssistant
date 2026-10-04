# skills/stocks/journal.py
# Дневник сделок: файл, голос, Telegram; средняя цена, зафиксированный результат, дневной отчёт.

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import common
from .common import _JOURNAL_HINTS, _lots_phrase, _quotation_to_float

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")
_TRADE_HISTORY_KEEP = 5000
_DAILY_REPORT_HOUR = 19


def _read_trades(path: str | None = None) -> list[dict[str, Any]]:
    path = path or common._TRADE_PATH
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("trades") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _write_trades(trades: list[dict[str, Any]], path: str | None = None) -> None:
    path = path or common._TRADE_PATH
    payload = {"trades": trades[-_TRADE_HISTORY_KEEP:]}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Биржа] не записал дневник: %s", exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _rub_text(amount: float, signed: bool = False) -> str:
    text = f"{amount:+,.0f}" if signed else f"{amount:,.0f}"
    return text.replace(",", " ") + " ₽"


def format_trade_line(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "").strip()
    side = "купил" if str(entry.get("side") or "") == "buy" else "продал"
    name = str(entry.get("name") or entry.get("ticker") or "")
    ticker = str(entry.get("ticker") or "")
    lots = entry.get("lots") or 0
    price = entry.get("price") or 0
    price_bit = f" по {price:g} ₽" if price else ""
    reason = str(entry.get("reason") or "")
    reason_bit = f" ({reason})" if reason else ""
    when = f"{ts} " if ts else ""
    return f"{when}{side} {_lots_phrase(int(lots))}: {name} ({ticker}){price_bit}{reason_bit}"


def format_journal(trades: list[dict[str, Any]] | None = None, limit: int = 8) -> str:
    rows = trades if trades is not None else _read_trades()
    if not rows:
        return ""
    lines = [format_trade_line(item) for item in rows[-limit:]]
    return "Дневник сделок:\n" + "\n".join(lines)


def _wants_journal(text: str) -> bool:
    return any(hint in text for hint in _JOURNAL_HINTS)


def fill_price(data: dict[str, Any] | None, *, lot: int, quote_price: float) -> float:
    """Цена одной бумаги из ответа PostOrder; нет её — котировка.

    Если брокер прислал цену лота (≈ котировка × лот), делим на лот.
    """
    px = _quotation_to_float((data or {}).get("executedOrderPrice"))
    quote = float(quote_price or 0)
    if px <= 0:
        return quote
    if lot > 1 and quote > 0 and abs(px / (quote * lot) - 1.0) < 0.2:
        return px / lot
    return px


def order_commission(data: dict[str, Any] | None) -> float:
    data = data or {}
    paid = _quotation_to_float(data.get("executedCommission"))
    return paid if paid > 0 else _quotation_to_float(data.get("initialCommission"))


def _entry_qty(entry: dict[str, Any]) -> float:
    """Бумаги в сделке. У старых записей qty нет — считаем лотами (для лота 1 это то же самое)."""
    try:
        qty = float(entry.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty > 0:
        return qty
    try:
        return float(entry.get("lots") or 0)
    except (TypeError, ValueError):
        return 0.0


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
    try:
        now = datetime.now(_MSK)
        parsed = datetime.strptime(f"{ts} {now.year}", "%d.%m %H:%M %Y").replace(tzinfo=_MSK)
        if parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed.timestamp()
    except ValueError:
        return None


def _walk_lots(rows: list[dict[str, Any]]):
    """Средняя цена по журналу. Отдаёт (запись, лоты после, себестоимость проданного, выручка)."""
    lots: dict[str, dict[str, float]] = {}
    for entry in rows:
        ticker = str(entry.get("ticker") or "").upper()
        side = str(entry.get("side") or "").lower()
        qty = _entry_qty(entry)
        price = float(entry.get("price") or 0)
        if not ticker or qty <= 0 or price <= 0:
            continue
        fee = float(entry.get("commission") or 0)
        lot = lots.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        if side == "buy":
            lot["qty"] += qty
            lot["cost"] += qty * price + fee
            continue
        if side != "sell" or lot["qty"] <= 0:
            continue
        sell_qty = min(qty, lot["qty"])
        cost_sold = lot["cost"] / lot["qty"] * sell_qty
        proceeds = (qty * price - fee) * (sell_qty / qty)
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-9:
            lot["qty"] = 0.0
            lot["cost"] = 0.0
        yield entry, lots, cost_sold, proceeds
    yield None, lots, 0.0, 0.0


def open_avg_costs(trades: list[dict[str, Any]] | None = None) -> dict[str, float]:
    rows = trades if trades is not None else _read_trades()
    lots: dict[str, dict[str, float]] = {}
    for _entry, lots, _cost, _proceeds in _walk_lots(rows):
        pass
    return {t: lot["cost"] / lot["qty"] for t, lot in lots.items() if lot["qty"] > 0}


def realized_pnl(trades: list[dict[str, Any]] | None = None) -> float | None:
    """Зафиксированный результат по закрытым сделкам журнала, с комиссией."""
    rows = trades if trades is not None else _read_trades()
    if not rows:
        return None
    total = 0.0
    for entry, _lots, cost, proceeds in _walk_lots(rows):
        if entry is not None:
            total += proceeds - cost
    return round(total, 2)


def cooldown_tickers(
    hours: float,
    trades: list[dict[str, Any]] | None = None,
    *,
    now: float | None = None,
) -> set[str]:
    """Убыточная продажа или выход по стопу/трейлу за последние hours — не берём снова."""
    from .desk_policy import EXIT_REASONS

    rows = trades if trades is not None else _read_trades()
    cutoff = (now if now is not None else time.time()) - max(0.0, hours) * 3600.0
    cool: set[str] = set()
    for entry, _lots, cost, proceeds in _walk_lots(rows):
        if entry is None:
            continue
        epoch = _entry_epoch(entry)
        if epoch is not None and epoch >= cutoff and proceeds < cost:
            cool.add(str(entry.get("ticker") or "").upper())
    for entry in rows:
        if str(entry.get("reason") or "") not in EXIT_REASONS:
            continue
        epoch = _entry_epoch(entry)
        if epoch is not None and epoch >= cutoff:
            cool.add(str(entry.get("ticker") or "").upper())
    return cool


def recently_bought(
    hours: float,
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
        if epoch is not None and start.timestamp() <= epoch < end.timestamp():
            out.append(entry)
    return out


def format_daily_pnl_report(
    trades: list[dict[str, Any]] | None = None,
    *,
    day: datetime | None = None,
) -> str:
    day = day or datetime.now(_MSK)
    rows = trades if trades is not None else _read_trades()
    day_trades = trades_on_msk_day(day, rows)
    buys = sum(1 for t in day_trades if str(t.get("side")) == "buy")
    sells = len(day_trades) - buys
    volume = sum(_entry_qty(t) * float(t.get("price") or 0) for t in day_trades)
    fees = sum(float(t.get("commission") or 0) for t in day_trades)
    lines = [
        f"Акции за {day.strftime('%d.%m')}: сделок {len(day_trades)} "
        f"(покупок {buys}, продаж {sells}), оборот {_rub_text(volume)}, комиссия {_rub_text(fees)}.",
    ]
    realized = realized_pnl(rows)
    if realized is not None:
        lines.append(f"Зафиксировано за всё время: {_rub_text(realized, signed=True)}.")
    if day_trades:
        lines.append("Последние:")
        lines.extend(format_trade_line(item) for item in day_trades[-5:])
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


def should_send_daily_report(*, now: datetime | None = None, path: str | None = None) -> bool:
    """Один отчёт в торговый день МСК после основной сессии, если были сделки."""
    now = now or datetime.now(_MSK)
    if now.weekday() >= 5 or now.hour < _DAILY_REPORT_HOUR:
        return False
    if _read_daily_meta(path).get("last_report_day") == now.strftime("%Y-%m-%d"):
        return False
    return bool(trades_on_msk_day(now))


def mark_daily_report_sent(*, now: datetime | None = None, path: str | None = None) -> None:
    now = now or datetime.now(_MSK)
    path = path or common._DAILY_PATH
    meta = _read_daily_meta(path)
    meta["last_report_day"] = now.strftime("%Y-%m-%d")
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Биржа] не записал отметку отчёта: %s", exc)


class StocksJournalMixin:
    def _run_journal(self, channel: str = "voice") -> str:
        text = format_journal() or "Дневник пуст: сделок ещё не было."
        realized = realized_pnl()
        if realized is not None and format_journal():
            text += f"\nЗафиксировано за всё время: {_rub_text(realized, signed=True)}."
        if channel == "telegram":
            return text
        if common.telegram_configured():
            common.send_telegram_notification(text, background=False)
            return "Отправил дневник в телеграм."
        return text

    def _journal_trade(
        self,
        ticker: str,
        direction: str,
        lots: int,
        spoken: str,
        *,
        data: dict[str, Any] | None = None,
        reason: str = "",
        notify: bool = True,
    ) -> None:
        ticker = ticker.upper()
        quote_price = 0.0
        try:
            _name, quote_price, _pct = self._quote(ticker)
        except Exception:
            quote_price = 0.0
        try:
            lot = max(int(self._lot_size(ticker)), 1)
        except Exception:
            lot = 1
        try:
            done = int(float((data or {}).get("lotsExecuted") or 0))
        except (TypeError, ValueError):
            done = 0
        lots = done if done > 0 else int(lots)
        price = fill_price(data, lot=lot, quote_price=quote_price)
        now = datetime.now(_MSK)
        entry: dict[str, Any] = {
            "ts": now.strftime("%d.%m %H:%M"),
            "ts_epoch": int(now.timestamp()),
            "ticker": ticker,
            "name": spoken,
            "side": "buy" if direction == "ORDER_DIRECTION_BUY" else "sell",
            "lots": lots,
            "qty": lots * lot,
            "price": round(float(price or 0), 4),
            "commission": round(order_commission(data), 2),
        }
        if reason:
            entry["reason"] = reason
        trades = _read_trades()
        trades.append(entry)
        _write_trades(trades)
        try:
            import conky_markets
            conky_markets.poke_refresh()
        except Exception:
            pass
        line = "Дневник: " + format_trade_line(entry)
        logger.info("[Биржа] %s", line)
        if notify and common.telegram_configured():
            common.send_telegram_notification(line, background=True)
