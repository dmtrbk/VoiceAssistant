# skills/stocks/trend.py
# Стол в режиме тренда (STOCKS_STRATEGY=trend): пробой 55-дневного максимума, выход ниже 10-дневного минимума.

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import journal as stocks_journal
from .common import _MOEX, _park_ticker, read_alloc_state, write_alloc_state
from .desk_policy import (
    COOLDOWN_HOURS,
    TREND_SLOTS,
    TREND_TICKERS,
    WATCH_PERIOD_CALM,
    is_risk_off,
    pick_trend_entries,
    trend_entry_window,
    trend_exit_reason,
    trend_levels,
    trend_slot_lots,
)

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")
_HISTORY_DAYS = 120


class StocksTrendMixin:
    def _trend_levels(self, ticker: str) -> dict[str, float] | None:
        """Канал по дневным свечам ISS, раз в день на бумагу."""
        today = datetime.now(_MSK).date().isoformat()
        cache: dict[str, tuple[str, dict[str, float] | None]] = self._trend_levels_cache
        hit = cache.get(ticker)
        if hit and hit[0] == today:
            return hit[1]
        start = (datetime.now(_MSK).date() - timedelta(days=_HISTORY_DAYS)).isoformat()
        data = self._get(
            f"{_MOEX}/boards/TQBR/securities/{ticker}/candles.json",
            {"iss.meta": "off", "interval": "24", "from": start},
            f"moex:candles24:{ticker}:{today}",
        )
        days = [
            (str(row.get("begin") or "")[:10], float(row.get("high") or 0), float(row.get("low") or 0))
            for row in self._iss_rows(data.get("candles"))
        ]
        levels = trend_levels(days, today=today)
        cache[ticker] = (today, levels)
        return levels

    def _trend_cycle(self, silent: bool = True) -> str:
        """Выходы по стопу и каналу, затем входы на пробое в свободные слоты."""
        state = read_alloc_state() or {}
        if not self._token:
            return "Режим тренда: торгового ключа нет."
        if not self._market_open():
            return "рынок закрыт"
        self._watch_period_sec = WATCH_PERIOD_CALM
        parts, sold, held = self._trend_exits()
        why, bought = self._trend_entries(held, sold)
        parts.extend(bought)
        if sold or bought:
            self._bust_broker_cache()
        self._park_idle()
        write_alloc_state(
            {}, why=why, watch_trades=list(state.get("watch_trades") or []), desk_ts=time.time()
        )
        result = " ".join([f"тренд: {why}.", *parts])
        if silent:
            logger.info("[Биржа] %s", result)
        return result

    def _trend_exits(self) -> tuple[list[str], set[str], set[str]]:
        """Продаёт бумаги стола ниже канала или по стопу. Возвращает (ответы, проданные, держим)."""
        positions, _day, _total = self._positions()
        park = _park_ticker()
        self._ensure_desk_bought({p["ticker"] for p in positions} - {park})
        try:
            avg_costs = stocks_journal.open_avg_costs()
        except Exception:
            avg_costs = {}
        parts: list[str] = []
        sold: set[str] = set()
        held: set[str] = set()
        for pos in positions:
            ticker = pos["ticker"]
            price = float(pos.get("price") or 0)
            qty = float(pos.get("qty") or 0)
            if ticker == park or qty <= 0:
                continue
            held.add(ticker)
            if self._is_owner_position(ticker) or price <= 0:
                continue
            entry = float(pos.get("avg") or 0) or float(avg_costs.get(ticker) or 0) or price
            try:
                levels = self._trend_levels(ticker)
            except Exception as exc:
                logger.info("[Биржа] тренд: канал %s: %s", ticker, exc)
                levels = None
            floor = levels["floor"] if levels else None
            reason = trend_exit_reason(price=price, entry=entry, floor=floor)
            logger.info(
                "[Биржа] тренд %s: цена %.4g, вход %.4g, низ канала %s",
                ticker, price, entry, f"{floor:.4g}" if floor else "—",
            )
            if not reason:
                continue
            try:
                _buy_max, sell_max = self._max_lots(ticker)
                if sell_max <= 0:
                    continue
                parts.append(self._place_order(ticker, "ORDER_DIRECTION_SELL", sell_max, reason=reason))
                sold.add(ticker)
                held.discard(ticker)
                time.sleep(0.6)
            except Exception as exc:
                logger.warning("[Биржа] тренд: выход %s: %s", ticker, exc)
        return parts, sold, held

    def _trend_entries(self, held: set[str], sold: set[str]) -> tuple[str, list[str]]:
        """(пояснение, ответы по покупкам)."""
        in_slots = [t for t in TREND_TICKERS if t in held and not self._is_owner_position(t)]
        free = TREND_SLOTS - len(in_slots)
        holding = f"держу {', '.join(in_slots)}" if in_slots else "позиций нет"
        if free <= 0:
            return f"{holding}, слоты заняты", []
        if not trend_entry_window(datetime.now(_MSK)):
            return f"{holding}, входы только в основную сессию", []
        week, day = self._imoex_regime()
        if is_risk_off(week=week, day=day):
            return f"{holding}. {self._regime_phrase(week, day)}", []
        try:
            cooldown = stocks_journal.cooldown_tickers(COOLDOWN_HOURS)
        except Exception:
            cooldown = set()
        try:
            prices = {row["ticker"]: float(row["price"]) for row in self._tqbr_tape()}
        except Exception as exc:
            logger.warning("[Биржа] тренд: лента TQBR: %s", exc)
            return f"{holding}, ленты нет", []
        rows: list[dict[str, Any]] = []
        for ticker in TREND_TICKERS:
            if ticker in held or ticker in sold or ticker in cooldown or self._is_buy_blocked(ticker):
                continue
            price = prices.get(ticker)
            if not price:
                continue
            try:
                levels = self._trend_levels(ticker)
            except Exception as exc:
                logger.info("[Биржа] тренд: канал %s: %s", ticker, exc)
                continue
            if levels:
                rows.append({"ticker": ticker, "price": price, "top": levels["top"]})
        picks = pick_trend_entries(rows, free)
        if not picks:
            return f"{holding}, пробоев нет", []
        equity = self._trend_equity()
        parts: list[str] = []
        for row in picks:
            ticker = row["ticker"]
            try:
                lot = self._lot_size(ticker)
                self._park_release(equity / TREND_SLOTS)
                lots = trend_slot_lots(
                    equity=equity, cash=self._broker_cash(), price=row["price"], lot=lot
                )
                buy_max, _sell_max = self._max_lots(ticker)
                lots = min(lots, buy_max)
                if lots <= 0:
                    logger.info("[Биржа] тренд: на %s не хватает денег", ticker)
                    continue
                logger.info(
                    "[Биржа] тренд: пробой %s %.4g > %.4g, беру %d лот.",
                    ticker, row["price"], row["top"], lots,
                )
                parts.append(self._place_order(ticker, "ORDER_DIRECTION_BUY", lots, reason="пробой"))
                self._bust_broker_cache()
                time.sleep(0.6)
            except Exception as exc:
                logger.warning("[Биржа] тренд: вход %s: %s", ticker, exc)
        names = ", ".join(r["ticker"] for r in picks)
        return f"{holding}, пробой: {names}", parts

    def _trend_equity(self) -> float:
        """Деньги стола: кэш, фонд паркинга и бумаги стола, без бумаг хозяина."""
        positions, _day, _total = self._positions()
        total = self._broker_cash()
        for pos in positions:
            if self._is_owner_position(pos["ticker"]) and pos["ticker"] != _park_ticker():
                continue
            total += float(pos.get("price") or 0) * float(pos.get("qty") or 0)
        return max(total, 0.0)
