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
    payload = {"trades": trades[-200:], "lifetime_pnl_usd": life}
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
        realized += proceeds - cost_sold
        lot["qty"] -= sell_qty
        lot["cost"] -= cost_sold
        if lot["qty"] < 1e-12:
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


def _mark_prices_from_wallet() -> dict[str, float]:
    try:
        from skills.crypto import CryptoSkill

        skill = CryptoSkill()
        skill._reload_env()
        if not skill._api_key:
            return {}
        _cash, positions = skill._wallet()
        out: dict[str, float] = {}
        for pos in positions:
            ticker = str(pos.get("ticker") or "").upper()
            price = float(pos.get("price") or 0)
            if ticker and price > 0:
                out[ticker] = price
        return out
    except Exception as exc:
        logger.warning("[Крипта] цены для lifetime: %s", exc)
        return {}


def recompute_lifetime_pnl(
    mark_prices: dict[str, float] | None = None,
    path: str | None = None,
) -> float | None:
    """Пересчитать и перезаписать одну цифру lifetime_pnl_usd."""
    trades = _read_trades(path)
    prices = mark_prices if mark_prices is not None else _mark_prices_from_wallet()
    value = compute_lifetime_from_trades(trades, prices)
    _write_trades(trades, path=path, lifetime_pnl_usd=value)
    return value


def format_trade_line(entry: dict[str, Any]) -> str:
    ts = str(entry.get("ts") or "").strip()
    side = "купил" if str(entry.get("side") or "") == "buy" else "продал"
    name = str(entry.get("name") or entry.get("ticker") or "")
    ticker = str(entry.get("ticker") or "")
    quote = entry.get("quote") or 0
    price = entry.get("price") or 0
    quote_bit = f" на {_format_usd(float(quote))}" if quote else ""
    price_bit = f" по {price:g}" if price else ""
    when = f"{ts} " if ts else ""
    return f"{when}{side} {name} ({ticker}){quote_bit}{price_bit}"


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

    def _journal_trade(self, ticker: str, side: str, quote: float, price: float, spoken: str) -> None:
        now = datetime.now(_MSK)
        entry = {
            "ts": now.strftime("%d.%m %H:%M"),
            "ts_epoch": int(now.timestamp()),
            "ticker": ticker,
            "name": spoken,
            "side": "buy" if side == "Buy" else "sell",
            "quote": round(float(quote or 0), 2),
            "price": round(float(price or 0), 6),
        }
        trades = _read_trades()
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
