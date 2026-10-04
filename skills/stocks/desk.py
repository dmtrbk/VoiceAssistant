# skills/stocks/desk.py
# Фонд: сигналы Т-Инвест, режим IMOEX, ребаланс в коридоре, выходы и паркинг кэша.

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import common
from . import journal as stocks_journal
from .common import (
    _API_BUY_BLOCKED,
    _DESK_PERIOD_SEC,
    _IMOEX_HISTORY,
    _IMOEX_NOW,
    _MOEX,
    _MOEX_BOARDS,
    _SIGNAL_MAX_NAMES,
    _SIGNAL_MAX_SMALL,
    _SIGNAL_PAGE,
    _SIGNALS,
    _SMALL_EQUITY_RUB,
    _WATCH_PERIOD_SEC,
    _buy_block_reason,
    _min_trade_rub,
    _park_enabled,
    _park_ticker,
    _read_ticker_set,
    _signal_is_buy,
    _signal_weight,
    _write_ticker_set,
    read_alloc_state,
    read_trail_state,
    write_alloc_state,
    write_trail_state,
)
from .desk_policy import (
    COOLDOWN_HOURS,
    MIN_HOLD_HOURS,
    apply_cash_floor,
    cash_floor_pct,
    exit_reason,
    index_changes,
    is_risk_off,
    park_amount_rub,
    park_release_lots,
    should_rebalance_leg,
    update_trail_leg,
    watch_rate_ok,
)

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")


class StocksDeskMixin:
    def _ensure_desk_loop(self) -> None:
        with common._desk_init_lock:
            if common._desk_loop_started:
                return
            self._desk_thread = threading.Thread(
                target=self._desk_loop,
                name="stocks-desk",
                daemon=True,
            )
            self._desk_thread.start()
            common._desk_loop_started = True

    def on_disabled(self) -> None:
        self._desk_enabled.clear()

    def on_enabled(self) -> None:
        self._desk_enabled.set()
        self.start_background()

    def start_background(self) -> None:
        """Фоновый стол — после старта ассистента, не при импорте навыка."""
        self._ensure_desk_loop()
    def _run_advisor(self, channel: str = "voice") -> str:
        """Сигналы → Groq → Telegram. Заявки не ставит."""
        import signal_advisor

        send_tg = common.telegram_configured() and channel != "telegram"
        try:
            text = signal_advisor.run(skill=self, to_telegram=send_tg, to_stdout=False)
        except RuntimeError as exc:
            if "нет токена" in str(exc):
                return "Без ключа брокера сигналы не достать."
            raise
        if channel == "telegram":
            return text
        if common.telegram_configured():
            return "Отправил рекомендацию в телеграм."
        return text
    def _held_map(self) -> dict[str, float]:
        held: dict[str, float] = {}
        if self._token:
            positions, _day, _total = self._safe_positions()
            for item in positions:
                held[item["ticker"]] = float(item.get("qty") or 0)
            return held
        for item in self._load_book():
            held[item["ticker"]] = float(item.get("qty") or 0)
        return held

    def _equity_estimate(self, tape: list[dict[str, Any]] | None = None) -> float:
        prices = {row["ticker"]: row["price"] for row in (tape or [])}
        held = self._held_map()
        total = self._broker_cash()
        for ticker, qty in held.items():
            price = prices.get(ticker)
            if price is None:
                try:
                    _name, price, _pct = self._moex_quote(ticker)
                except Exception:
                    price = 0.0
            total += qty * float(price or 0)
        return max(total, 1.0)

    def _tqbr_tape(self) -> list[dict[str, Any]]:
        data = self._get(
            f"{_MOEX}/boards/TQBR/securities.json",
            {"iss.meta": "off", "iss.only": "securities,marketdata"},
            "moex:tqbr:tape",
        )
        sec_rows = {row.get("SECID"): row for row in self._iss_rows(data.get("securities"))}
        tape: list[dict[str, Any]] = []
        for row in self._iss_rows(data.get("marketdata")):
            ticker = str(row.get("SECID") or "").upper()
            last = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE")
            if not ticker or last in (None, "", 0, 0.0):
                continue
            sec = sec_rows.get(ticker) or {}
            lot = sec.get("LOTSIZE") or 1
            try:
                lot = max(int(lot), 1)
            except (TypeError, ValueError):
                lot = 1
            name = str(sec.get("SHORTNAME") or sec.get("SECNAME") or ticker)
            pct = float(row.get("LASTTOPREVPRICE") or 0)
            value = float(row.get("VALTODAY") or row.get("VALUE") or 0)
            tape.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "price": float(last),
                    "pct": pct,
                    "lot": lot,
                    "value": value,
                }
            )
            self._remember_name(ticker, name.split()[0] if name else ticker)
        return tape

    def _desk_candidates(self) -> list[dict[str, Any]]:
        try:
            tape = self._tqbr_tape()
        except Exception as exc:
            logger.warning("[Биржа] лента TQBR: %s", exc)
            tape = []
        held = self._held_map()
        equity = self._equity_estimate(tape)
        held.pop(_park_ticker(), None)
        from_tape = {row["ticker"]: row for row in tape}
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            ticker = row["ticker"]
            if ticker in seen:
                return
            seen.add(ticker)
            chosen.append(row)

        for ticker, qty in held.items():
            row = from_tape.get(ticker)
            if row is None:
                try:
                    name, price, pct = self._moex_quote(ticker)
                    row = {"ticker": ticker, "name": name, "price": price, "pct": pct, "lot": 1, "value": 0}
                except Exception:
                    continue
            row = dict(row)
            row["held"] = qty
            add(row)

        # Мелкий фонд: не гоняемся за всей лентой TQBR — только то, что уже есть
        # и watchlist, если лот реально влезает в капитал.
        if equity < _SMALL_EQUITY_RUB:
            for ticker in self._watchlist:
                ticker = ticker.upper()
                if ticker in seen:
                    continue
                row = from_tape.get(ticker)
                if row is None:
                    try:
                        name, price, pct = self._moex_quote(ticker)
                        row = {
                            "ticker": ticker,
                            "name": name,
                            "price": price,
                            "pct": pct,
                            "lot": 1,
                            "value": 0,
                        }
                    except Exception:
                        continue
                lot = max(int(row.get("lot") or 1), 1)
                if row["price"] * lot > equity * 0.98:
                    continue
                add(dict(row, held=held.get(ticker, 0)))
            return chosen

        affordable = [
            row
            for row in tape
            if row["ticker"] not in seen
            and row["price"] * row["lot"] <= equity * 0.98
            and row["value"] >= 500_000
        ]
        affordable.sort(key=lambda row: abs(row["pct"]), reverse=True)
        for row in affordable:
            if len(chosen) >= 12:
                break
            add(dict(row, held=0))
        return chosen

    def _lot_size(self, ticker: str) -> int:
        ticker = ticker.upper()
        if self._token:
            try:
                inst = self._instrument(None, ticker)
                lot = int(inst.get("lot") or 0)
                if lot > 0:
                    return lot
            except Exception:
                pass
        try:
            for row in self._tqbr_tape():
                if row.get("ticker") == ticker:
                    return int(row.get("lot") or 1)
        except Exception:
            pass
        return 1

    def _is_buy_blocked(self, ticker: str) -> bool:
        ticker = ticker.upper()
        return ticker in _API_BUY_BLOCKED or ticker in self._buy_blocked

    def _save_holds(self) -> None:
        _write_ticker_set(common._HOLD_PATH, self._manual_holds)

    def _mark_desk_bought(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._desk_bought.add(ticker)
        self._desk_bought_ready = True
        _write_ticker_set(common._BOUGHT_PATH, self._desk_bought)

    def _ensure_desk_bought(self, held: set[str]) -> None:
        """Если bought.json нет — всё на балансе чужое, пока стол сам не купит."""
        if self._desk_bought_ready:
            return
        self._desk_bought = set()
        self._desk_bought_ready = True
        _write_ticker_set(common._BOUGHT_PATH, self._desk_bought)
        if held:
            logger.info(
                "[Биржа] нет bought.json — %d позиций на балансе не трогаю как чужие",
                len(held),
            )

    def _is_owner_position(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker in self._manual_holds:
            return True
        return self._desk_bought_ready and ticker not in self._desk_bought
    def _filter_buy_alloc(
        self,
        alloc: dict[str, float],
        candidates: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Убирает бумаги, которые API не покупает; оставшиеся доли растягивает до прежней суммы.

        Сумма цели может быть меньше 100% — это доля кэша, её не съедаем.
        """
        cleaned: dict[str, float] = {}
        wanted = 0.0
        for ticker, pct in (alloc or {}).items():
            try:
                wanted += max(0.0, float(pct))
            except (TypeError, ValueError):
                pass
            ticker_u = str(ticker).upper().strip()
            if not ticker_u or self._is_buy_blocked(ticker_u):
                continue
            try:
                val = float(pct)
            except (TypeError, ValueError):
                continue
            if val > 0:
                cleaned[ticker_u] = val
        if not cleaned:
            return self._fallback_allocation(candidates)
        total = sum(cleaned.values())
        if total <= 0:
            return self._fallback_allocation(candidates)
        scale = min(wanted, 100.0) / total
        return {key: round(val * scale, 1) for key, val in cleaned.items()}

    def _fallback_allocation(self, candidates: list[dict[str, Any]]) -> dict[str, float]:
        """Фолбек: первая бумага из списка, которую API позволяет купить."""
        for row in candidates:
            ticker = str(row.get("ticker") or "").upper()
            if ticker and not self._is_buy_blocked(ticker):
                return {ticker: 100.0}
        return {}

    def _fetch_buy_signals(self) -> list[dict[str, Any]]:
        if not self._token:
            return []
        try:
            data = self._post(
                self._url(_SIGNALS),
                {
                    "direction": "SIGNAL_DIRECTION_BUY",
                    "active": "SIGNAL_STATE_ACTIVE",
                    "paging": {"limit": _SIGNAL_PAGE, "pageNumber": 0},
                },
                cache_key="signals:buy:active",
            )
        except Exception as exc:
            logger.warning("[Биржа] сигналы Т-Инвест: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in data.get("signals") or []:
            if isinstance(raw, dict) and _signal_is_buy(raw.get("direction")):
                out.append(raw)
        return out

    def _signal_lot_cost(
        self,
        ticker: str,
        inst: dict[str, Any],
        lot_cost: dict[str, float],
    ) -> float:
        if ticker in lot_cost:
            return lot_cost[ticker]
        lot = max(int(inst.get("lot") or 1), 1)
        try:
            _name, price, _pct = self._quote(ticker)
        except Exception:
            return 0.0
        cost = float(price or 0) * lot
        if cost > 0:
            lot_cost[ticker] = cost
        return cost

    def _recommend_manual_buys(self, tickers: list[str]) -> None:
        """Раз: сигнал есть, через API не купить — написать хозяину в Telegram."""
        fresh: list[str] = []
        for ticker in tickers:
            ticker_u = str(ticker or "").upper().strip()
            if not ticker_u or ticker_u in self._manual_tips_sent:
                continue
            self._manual_tips_sent.add(ticker_u)
            self._manual_holds.add(ticker_u)
            fresh.append(ticker_u)
        if not fresh:
            return
        self._save_holds()
        lines = [
            f"{self._spoken_name(ticker)} ({ticker}) — {_buy_block_reason(ticker)}"
            for ticker in fresh
        ]
        if len(lines) == 1:
            text = (
                f"Сигнал Т-Инвест: {lines[0]}. "
                "Сам не куплю. Если согласен — возьми в приложении."
            )
        else:
            text = (
                "Сигналы, которые сам не куплю:\n"
                + "\n".join(f"• {line}" for line in lines)
                + "\nЕсли согласен — возьми в приложении."
            )
        logger.info("[Биржа] рекомендация хозяину: %s", text)
        if common.telegram_configured():
            common.send_telegram_notification(text)

    def _desk_choose(
        self,
        candidates: list[dict[str, Any]],
        equity: float | None = None,
        cooldown: set[str] | None = None,
    ) -> dict[str, float]:
        if equity is None:
            equity = self._equity_estimate(candidates)
        small = equity < _SMALL_EQUITY_RUB
        held_now = {str(row.get("ticker") or "").upper() for row in candidates if float(row.get("held") or 0) > 0}
        skip = {str(t).upper() for t in (cooldown or set())} - held_now
        skip.add(_park_ticker())
        lot_cost = {
            str(row["ticker"]).upper(): float(row.get("price") or 0) * max(int(row.get("lot") or 1), 1)
            for row in candidates
            if row.get("ticker")
        }
        scores: dict[str, float] = {}
        blocked_scores: dict[str, float] = {}
        for sig in self._fetch_buy_signals():
            uid = str(sig.get("instrumentUid") or sig.get("instrument_uid") or "").strip()
            if not uid:
                continue
            inst = self._instrument(None, None, uid=uid)
            ticker = str(inst.get("ticker") or "").upper()
            if not ticker or ticker in skip:
                continue
            class_code = str(inst.get("classCode") or "")
            if class_code and class_code not in _MOEX_BOARDS:
                continue
            kind = str(inst.get("instrumentKind") or inst.get("instrumentType") or "").upper()
            if kind and "SHARE" not in kind and "ETF" not in kind:
                continue
            cost = self._signal_lot_cost(ticker, inst, lot_cost)
            if cost <= 0 or cost > equity * 0.98:
                continue
            weight = _signal_weight(sig.get("probability"))
            self._remember_name(ticker, self._spoken_name(ticker, inst))
            blocked = self._is_buy_blocked(ticker) or inst.get("apiTradeAvailableFlag") is False
            if blocked:
                blocked_scores[ticker] = blocked_scores.get(ticker, 0.0) + weight
                continue
            scores[ticker] = scores.get(ticker, 0.0) + weight

        limit = _SIGNAL_MAX_SMALL if small else _SIGNAL_MAX_NAMES
        ranked_all = sorted(
            list(scores.items()) + list(blocked_scores.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        tips = [ticker for ticker, _weight in ranked_all[:limit] if ticker in blocked_scores]
        if tips:
            self._recommend_manual_buys(tips)

        if not scores:
            logger.info("[Биржа] активных сигналов Т-Инвест нет, фоллбек")
            return self._fallback_allocation(candidates)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        picked = ranked[:limit]
        total = sum(value for _ticker, value in picked)
        if total <= 0:
            return self._fallback_allocation(candidates)
        alloc = {ticker: round((value / total) * 100.0, 1) for ticker, value in picked}
        logger.info("[Биржа] сигналы Т-Инвест: %s", alloc)
        return alloc

    def _rebalance_portfolio(self, target_alloc: dict[str, float], silent: bool = False) -> str:
        parts: list[str] = []
        acted = False
        target_alloc = self._filter_buy_alloc(
            target_alloc,
            [{"ticker": str(ticker)} for ticker in (target_alloc or {})],
        )
        if not target_alloc:
            result = "Нечего покупать: брокер не даёт эти бумаги через API."
            if silent:
                logger.info("[Биржа] авто-ребалансировка: %s", result)
            return result

        park = _park_ticker()
        target_alloc.pop(park, None)
        positions_list, _day, _total = self._safe_positions()
        positions = {p["ticker"]: p for p in positions_list}
        self._ensure_desk_bought(set(positions.keys()) - {park})
        cash = self._broker_cash()

        # Общая стоимость портфеля (Equity = Cash + Стоимость всех позиций, фонд паркинга тоже)
        total_pos_value = sum(p["price"] * p["qty"] for p in positions.values())
        equity = max(cash + total_pos_value, 1.0)
        min_trade_rub = _min_trade_rub(equity)
        day_chgs = self._day_changes()
        try:
            fresh = stocks_journal.recently_bought(MIN_HOLD_HOURS)
        except Exception:
            fresh = set()

        # Собираем актуальные цены и размер лотов для всех задействованных бумаг
        relevant_tickers = (set(positions.keys()) | set(target_alloc.keys())) - {park}
        prices: dict[str, float] = {}
        lotsizes: dict[str, int] = {}

        for ticker in relevant_tickers:
            if ticker in positions and positions[ticker].get("price", 0) > 0:
                prices[ticker] = positions[ticker]["price"]
            else:
                try:
                    _, p, _ = self._quote(ticker)
                    prices[ticker] = p
                except Exception:
                    prices[ticker] = 0.0
            lotsizes[ticker] = self._lot_size(ticker)

        # Целевая и текущая стоимость позиций
        target_values = {
            ticker: equity * (target_alloc.get(ticker, 0.0) / 100.0)
            for ticker in relevant_tickers
        }
        current_values = {
            ticker: positions.get(ticker, {}).get("qty", 0.0) * prices.get(ticker, 0.0)
            for ticker in relevant_tickers
        }

        # 1. Цикл ПРОДАЖИ: сокращаем или полностью закрываем позиции, чья доля выше целевой
        sell_candidates: list[tuple[str, float]] = []
        for ticker in relevant_tickers:
            diff = current_values[ticker] - target_values[ticker]
            if diff <= 0:
                continue
            if target_values[ticker] <= 0:
                # Полная ликвидация исключенных — тоже с порогом, чтобы мелкий фонд
                # не продавал всё в кэш, который потом не набирает лоты цели.
                if diff < min_trade_rub:
                    continue
                if ticker in fresh and not self._is_owner_position(ticker):
                    logger.info(
                        "[Биржа] %s куплен < %d ч назад — не ротирую",
                        ticker,
                        int(MIN_HOLD_HOURS),
                    )
                    continue
            elif not should_rebalance_leg(
                current_value=current_values[ticker],
                target_value=target_values[ticker],
                equity=equity,
                min_trade_rub=min_trade_rub,
                day_chg=day_chgs.get(ticker),
            ):
                continue
            sell_candidates.append((ticker, diff))

        sell_candidates.sort(key=lambda item: item[1], reverse=True)

        for ticker, excess_rub in sell_candidates:
            _buy_max, sell_max = self._max_lots(ticker)
            if sell_max <= 0:
                continue

            price = prices.get(ticker, 0.0)
            lot_size = lotsizes.get(ticker, 1)
            lot_cost = price * lot_size

            # Если бумаги нет в целевом портфеле — продаем весь доступный объем.
            # Бумаги хозяина (купил сам) не трогаем.
            if target_alloc.get(ticker, 0.0) <= 0:
                if self._is_owner_position(ticker):
                    continue
                lots_to_sell = sell_max
            else:
                lots_to_sell = int(excess_rub / lot_cost) if lot_cost > 0 else 0
                lots_to_sell = min(lots_to_sell, sell_max)

            if lots_to_sell > 0:
                try:
                    res = self._place_order(ticker, "ORDER_DIRECTION_SELL", lots_to_sell)
                    parts.append(res)
                    acted = True
                    time.sleep(0.6)
                except Exception as exc:
                    logger.warning("[Биржа] Ошибка при продаже %s: %s", ticker, exc)

        # После продаж пересчитываем позиции и кэш — иначе покупки идут по старым долям.
        if acted:
            time.sleep(1.0)
            self._bust_broker_cache()
            positions_list, _day, _total = self._safe_positions()
            positions = {item["ticker"]: item for item in positions_list}
            current_values = {
                ticker: positions.get(ticker, {}).get("qty", 0.0) * prices.get(ticker, 0.0)
                for ticker in relevant_tickers
            }

        # 2. Цикл ПОКУПКИ: принцип «Сначала считаем — потом покупаем»
        buy_candidates: list[tuple[str, float]] = []
        for ticker, target_pct in target_alloc.items():
            if self._is_buy_blocked(ticker):
                continue
            diff = target_values[ticker] - current_values.get(ticker, 0.0)
            if diff < min_trade_rub:
                continue
            if current_values.get(ticker, 0.0) > 0 and not should_rebalance_leg(
                current_value=current_values.get(ticker, 0.0),
                target_value=target_values[ticker],
                equity=equity,
                min_trade_rub=min_trade_rub,
            ):
                continue
            buy_candidates.append((ticker, diff))

        buy_candidates.sort(key=lambda item: item[1], reverse=True)
        if buy_candidates:
            self._park_release(sum(diff for _t, diff in buy_candidates))
        available_cash: float | None = self._broker_cash()
        if self._token and (available_cash or 0) < min_trade_rub:
            # После продаж кэш в GetPortfolio иногда ещё ноль; лимит — GetMaxLots.
            available_cash = None

        # Формируем предварительный план покупок с учетом доступного кэша
        planned_buys: list[tuple[str, int]] = []
        for ticker, need_rub in buy_candidates:
            price = prices.get(ticker, 0.0)
            lot_size = lotsizes.get(ticker, 1)
            lot_cost = price * lot_size
            if lot_cost <= 0:
                continue

            desired_lots = int(need_rub / lot_cost)
            if desired_lots <= 0:
                continue

            if available_cash is not None and desired_lots * lot_cost > available_cash:
                desired_lots = int(available_cash / lot_cost)

            if desired_lots > 0:
                if available_cash is not None:
                    available_cash -= desired_lots * lot_cost
                planned_buys.append((ticker, desired_lots))

        # Выполняем ордера по составленному плану покупок
        for ticker, planned_lots in planned_buys:
            buy_max, _sell_max = self._max_lots(ticker)
            lots_to_buy = min(planned_lots, buy_max)
            if lots_to_buy <= 0:
                continue

            try:
                res = self._place_order(ticker, "ORDER_DIRECTION_BUY", lots_to_buy)
                parts.append(res)
                acted = True
                time.sleep(0.6)
                self._bust_broker_cache()
            except Exception as exc:
                logger.warning("[Биржа] Ошибка при покупке %s: %s", ticker, exc)

        if not acted or not parts:
            if buy_candidates:
                result = "Цель есть, но свободных лотов пока не набралось."
            else:
                result = "Портфель уже сбалансирован в целевых долях."
        else:
            result = "Ребалансировал портфель: " + " ".join(parts)

        if silent:
            logger.info("[Биржа] авто-ребалансировка: %s", result)
            return result
        return result

    def _trade_auto(self, silent: bool = False) -> str:
        """Сам формирует сбалансированный портфель и ребалансирует фонд."""
        with common._TRADE_LOCK:
            return self._trade_auto_locked(silent)

    def _imoex_regime(self) -> tuple[float | None, float | None]:
        """(неделя %, сутки %) индекса Мосбиржи. Нет данных — None, режим считается обычным."""
        current: float | None = None
        day: float | None = None
        try:
            data = self._get(_IMOEX_NOW, {"iss.meta": "off", "iss.only": "marketdata"}, "moex:imoex:now")
            for row in self._iss_rows(data.get("marketdata")):
                value = row.get("CURRENTVALUE") or row.get("LASTVALUE")
                if value:
                    current = float(value)
                    if row.get("LASTCHANGEPRC") is not None:
                        day = float(row["LASTCHANGEPRC"])
                    break
        except Exception as exc:
            logger.info("[Биржа] IMOEX сейчас: %s", exc)
        history: list[tuple[str, float]] = []
        try:
            data = self._get(
                _IMOEX_HISTORY,
                {"iss.meta": "off", "iss.only": "history", "sort_order": "desc", "limit": "15"},
                "moex:imoex:hist",
            )
            history = [
                (str(row.get("TRADEDATE") or ""), float(row.get("CLOSE") or 0))
                for row in self._iss_rows(data.get("history"))
                if row.get("CLOSE")
            ]
        except Exception as exc:
            logger.info("[Биржа] IMOEX история: %s", exc)
        return index_changes(history, current, day, today=datetime.now(_MSK).date())

    @staticmethod
    def _regime_phrase(week: float | None, day: float | None) -> str:
        bits = []
        if week is not None:
            bits.append(f"за неделю {week:+.1f}%")
        if day is not None:
            bits.append(f"за день {day:+.1f}%")
        detail = f" (IMOEX {', '.join(bits)})" if bits else ""
        return f"Рынок слабый{detail} — новых бумаг не беру, купленные держу до стопа."

    def _day_changes(self) -> dict[str, float]:
        try:
            return {row["ticker"]: float(row.get("pct") or 0) for row in self._tqbr_tape()}
        except Exception as exc:
            logger.info("[Биржа] дневные изменения: %s", exc)
            return {}

    def _trade_auto_locked(self, silent: bool = False) -> str:
        candidates = self._desk_candidates()
        if not candidates:
            return "Нечего решать: лента пуста."
        state = read_alloc_state() or {}
        watch_trades = list(state.get("watch_trades") or [])
        week, day = self._imoex_regime()
        if is_risk_off(week=week, day=day):
            why = self._regime_phrase(week, day)
            logger.info("[Биржа] %s", why)
            write_alloc_state({}, why=why, risk_off=True, watch_trades=watch_trades)
            return why
        try:
            cooldown = stocks_journal.cooldown_tickers(COOLDOWN_HOURS)
        except Exception:
            cooldown = set()
        alloc = self._filter_buy_alloc(
            self._desk_choose(candidates, equity=self._equity_estimate(candidates), cooldown=cooldown),
            candidates,
        )
        if not alloc:
            write_alloc_state({}, why="нечего покупать через API", watch_trades=watch_trades)
            return "Нечего покупать: брокер не даёт эти бумаги через API."
        floor = cash_floor_pct(week=week, day=day)
        alloc = apply_cash_floor(alloc, floor)

        alloc_parts = []
        for ticker, pct in alloc.items():
            name = self._spoken_name(ticker)
            alloc_parts.append(f"{name} {int(round(pct))}%")
        alloc_desc = ", ".join(alloc_parts)
        if floor > 0:
            alloc_desc += f", кэш не меньше {int(floor)}%"
        logger.info("[Биржа] целевой портфель: %s", alloc_desc)
        write_alloc_state(alloc, why=alloc_desc, watch_trades=watch_trades)

        if not self._token:
            return f"Целевой портфель: {alloc_desc}. Торгового ключа нет, пока держу на бумаге."

        if not self._market_open():
            raise RuntimeError("market closed")

        result = self._rebalance_portfolio(alloc, silent=silent)
        self._park_idle()
        return result

    def _desk_watch(self, silent: bool = True) -> str:
        with common._TRADE_LOCK:
            return self._desk_watch_locked(silent)

    def _desk_watch_locked(self, silent: bool = True) -> str:
        """Дозор: стопы и трейл по бумагам стола, затем ребаланс к сохранённой цели."""
        state = read_alloc_state()
        if state is None:
            if silent:
                logger.info("[Биржа] дозор: нет цели — жду полный стол")
            return "нет цели"
        if not self._token:
            return "токена нет"
        if not self._market_open():
            return "рынок закрыт"
        parts, sold = self._desk_exits()
        alloc = {t: p for t, p in (state.get("alloc") or {}).items() if t not in sold}
        why = str(state.get("why") or "")
        risk_off = bool(state.get("risk_off"))
        watch_trades = list(state.get("watch_trades") or [])
        if alloc and not risk_off:
            week, day = self._imoex_regime()
            if is_risk_off(week=week, day=day):
                why = self._regime_phrase(week, day)
                risk_off = True
                alloc = {}
                logger.info("[Биржа] дозор: %s", why)
        if alloc:
            if watch_rate_ok(watch_trades):
                result = self._rebalance_portfolio(alloc, silent=False)
                if result.startswith("Ребалансировал"):
                    watch_trades.append(time.time())
                    parts.append(result)
            else:
                logger.info("[Биржа] дозор: лимит сделок за час")
        if sold or risk_off != bool(state.get("risk_off")) or len(watch_trades) != len(state.get("watch_trades") or []):
            write_alloc_state(alloc, why=why, risk_off=risk_off, watch_trades=watch_trades)
        result = "дозор: " + (" ".join(parts) if parts else "без сделок")
        if silent:
            logger.info("[Биржа] %s", result)
        return result

    def _desk_exits(self) -> tuple[list[str], set[str]]:
        """Стоп от средней цены и трейл от пика — по бумагам стола, без коридора и удержания."""
        positions, _day, _total = self._safe_positions()
        if not positions:
            return [], set()
        park = _park_ticker()
        cash = self._broker_cash()
        equity = max(cash + sum(p["price"] * p["qty"] for p in positions), 1.0)
        min_trade = _min_trade_rub(equity)
        self._ensure_desk_bought({p["ticker"] for p in positions} - {park})
        try:
            avg_costs = stocks_journal.open_avg_costs()
        except Exception:
            avg_costs = {}
        legs = read_trail_state()
        new_legs: dict[str, dict[str, Any]] = {}
        parts: list[str] = []
        sold: set[str] = set()
        for pos in positions:
            ticker = pos["ticker"]
            if ticker == park or self._is_owner_position(ticker):
                continue
            price = float(pos.get("price") or 0)
            qty = float(pos.get("qty") or 0)
            if price <= 0 or qty <= 0 or price * qty < min_trade:
                continue
            prev = legs.get(ticker) or {}
            entry = float(pos.get("avg") or 0) or float(avg_costs.get(ticker) or 0) or float(prev.get("entry") or 0) or price
            leg = update_trail_leg(
                price=price,
                entry=entry,
                high=float(prev.get("high") or 0) or None,
                armed=bool(prev.get("armed")),
            )
            keep = {"entry": leg["entry"], "high": leg["high"], "armed": leg["armed"]}
            reason = exit_reason(price=price, entry=entry, trail_hit=leg["hit"])
            if not reason:
                new_legs[ticker] = keep
                continue
            try:
                _buy_max, sell_max = self._max_lots(ticker)
                if sell_max <= 0:
                    new_legs[ticker] = keep
                    continue
                parts.append(self._place_order(ticker, "ORDER_DIRECTION_SELL", sell_max, reason=reason))
                sold.add(ticker)
                logger.info(
                    "[Биржа] выход %s (%s): вход %.4g, пик %.4g, цена %.4g, %+.1f%%",
                    ticker,
                    reason,
                    entry,
                    leg["high"],
                    price,
                    leg["gain_pct"],
                )
                time.sleep(0.6)
            except Exception as exc:
                logger.warning("[Биржа] выход %s: %s", ticker, exc)
                new_legs[ticker] = keep
        write_trail_state(new_legs)
        return parts, sold

    def _park_release(self, need_rub: float) -> bool:
        """Продать столько лотов фонда паркинга, чтобы хватило на покупки."""
        if not (_park_enabled() and self._token):
            return False
        park = _park_ticker()
        positions, _day, _total = self._safe_positions()
        pos = next((p for p in positions if p["ticker"] == park), None)
        if not pos:
            return False
        try:
            lot_cost = float(pos.get("price") or 0) * self._lot_size(park)
            _buy_max, sell_max = self._max_lots(park)
            lots = park_release_lots(
                need_rub=need_rub,
                cash=self._broker_cash(),
                lot_cost=lot_cost,
                held_lots=sell_max,
            )
            if lots <= 0:
                return False
            self._place_order(park, "ORDER_DIRECTION_SELL", lots, reason="паркинг", notify=False)
        except Exception as exc:
            logger.warning("[Биржа] паркинг: не вернул кэш из %s: %s", park, exc)
            return False
        logger.info("[Биржа] паркинг: вернул %d лотов %s под покупки", lots, park)
        time.sleep(1.0)
        self._bust_broker_cache()
        return True

    def _park_idle(self) -> None:
        """Свободный кэш сверх буфера — в фонд денежного рынка."""
        if not (_park_enabled() and self._token):
            return
        park = _park_ticker()
        if self._is_buy_blocked(park):
            return
        try:
            positions, _day, _total = self._safe_positions()
            cash = self._broker_cash()
            equity = cash + sum(p["price"] * p["qty"] for p in positions)
            amount = park_amount_rub(cash=cash, equity=equity)
            if amount <= 0:
                return
            _name, price, _pct = self._quote(park)
            lot_cost = float(price or 0) * self._lot_size(park)
            if lot_cost <= 0:
                return
            buy_max, _sell_max = self._max_lots(park)
            lots = min(int(amount // lot_cost), buy_max)
            if lots <= 0:
                return
            self._place_order(park, "ORDER_DIRECTION_BUY", lots, reason="паркинг", notify=False)
            logger.info("[Биржа] паркинг: %d лотов %s на %.0f ₽", lots, park, lots * lot_cost)
        except Exception as exc:
            logger.warning("[Биржа] паркинг в %s: %s", park, exc)

    def _maybe_daily_report(self) -> None:
        if not common.telegram_configured():
            return
        try:
            if not stocks_journal.should_send_daily_report():
                return
            common.send_telegram_notification(stocks_journal.format_daily_pnl_report(), background=False)
            stocks_journal.mark_daily_report_sent()
            logger.info("[Биржа] дневной отчёт отправлен.")
        except Exception as exc:
            logger.warning("[Биржа] дневной отчёт: %s", exc)

    def _desk_ready(self) -> bool:
        if not self._desk_enabled.is_set():
            return False
        if not common._auto_trade_enabled():
            return False
        try:
            from skill_settings import is_skill_enabled
            if not is_skill_enabled(self):
                self._desk_enabled.clear()
                return False
        except Exception:
            pass
        return True

    def _desk_loop(self) -> None:
        # Полный стол ~3 ч (сигналы), дозор ~15 мин к сохранённой цели. Вне сессии — пауза.
        self._desk_stop.wait(90)
        next_desk = 0.0
        next_watch = 0.0
        while not self._desk_stop.is_set():
            if not self._desk_ready():
                if self._desk_stop.wait(2.0):
                    return
                continue
            self._token = (
                os.getenv("TINKOFF_INVEST_TOKEN") or os.getenv("TINKOFF_TOKEN") or ""
            ).strip()
            now = time.time()
            if next_desk <= 0:
                if read_alloc_state() is None:
                    next_desk = now
                else:
                    next_desk = now + _DESK_PERIOD_SEC
                next_watch = now + _WATCH_PERIOD_SEC
            try:
                market_open = self._market_open()
            except Exception:
                market_open = False
            if not market_open:
                # После вечерней сессии: в отчёт попадают все сделки дня.
                self._maybe_daily_report()
                if self._desk_stop.wait(120.0):
                    return
                continue
            wake_at = min(next_desk, next_watch)
            while time.time() < wake_at:
                remaining = min(2.0, wake_at - time.time())
                if remaining <= 0:
                    break
                if self._desk_stop.wait(remaining):
                    return
                if not self._desk_ready():
                    break
            if not self._desk_ready():
                continue
            try:
                if not self._market_open():
                    continue
            except Exception:
                continue
            now = time.time()
            try:
                if now >= next_desk:
                    self._trade_auto(silent=True)
                    next_desk = time.time() + _DESK_PERIOD_SEC
                    next_watch = time.time() + _WATCH_PERIOD_SEC
                elif now >= next_watch:
                    self._desk_watch(silent=True)
                    next_watch = time.time() + _WATCH_PERIOD_SEC
            except Exception as exc:
                logger.warning("[Биржа] фоновый цикл торговли: %s", exc)
                next_watch = time.time() + _WATCH_PERIOD_SEC
