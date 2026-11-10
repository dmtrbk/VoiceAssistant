# skills/crypto/trades.py
# Голосовые заявки спота Bybit.

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import common
from .common import (
    _ALLIN_HINTS,
    _AMT_WORDS,
    _AUTO_HINTS,
    _BUY_HINTS,
    _MAX_HINTS,
    _MIN_QUOTE,
    _SELL_HINTS,
    _format_usd,
    _qty_str,
    _spoken,
)

logger = logging.getLogger(__name__)


def _sell_price_floor_from_exc(exc: BaseException | str) -> float | None:
    """Bybit на пампе: «Sell order price cannot be lower than 0.2928USDT»."""
    msg = str(exc or "")
    match = re.search(
        r"cannot be lower than\s*([0-9]+(?:\.[0-9]+)?)",
        msg,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        px = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def _cum_exec_fee_usdt(state: dict[str, Any], ticker: str, side: str) -> float:
    """cumExecFee заявки в USDT. Buy без feeCurrency — комиссия в монете."""
    try:
        fee = float(state.get("cumExecFee") or 0)
    except (TypeError, ValueError):
        return 0.0
    if fee <= 0:
        return 0.0
    try:
        qty = float(state.get("cumExecQty") or 0)
        quote = float(state.get("cumExecValue") or 0)
    except (TypeError, ValueError):
        qty = quote = 0.0
    px = quote / qty if qty else 0.0
    cur = str(state.get("feeCurrency") or "").upper()
    coin = str(ticker or "").upper()
    if cur in {"USDT", "USD"}:
        return fee
    if cur == coin or (not cur and str(side).lower() == "buy"):
        return fee * px if px > 0 else 0.0
    if cur:
        return fee * px if px > 0 else 0.0
    return fee


def _has_max_hint(text: str) -> bool:
    return any(re.search(rf"(?<![а-яa-z]){re.escape(word)}(?![а-яa-z])", text) for word in _MAX_HINTS)


def _trade_kind(text: str) -> str | None:
    if any(hint in text for hint in _AUTO_HINTS):
        return "auto"
    if any(hint in text for hint in _ALLIN_HINTS):
        return "allin"
    if any(hint in text for hint in _SELL_HINTS):
        return "sell"
    if any(hint in text for hint in _BUY_HINTS):
        return "buy"
    if "вложи" in text:
        return "allin" if _has_max_hint(text) else "buy"
    return None


def _extract_quote(text: str) -> float | None:
    """None — всё. 0 — минимум биржи. Иначе сумма в USDT («на 20 долларов»)."""
    if _has_max_hint(text):
        return None
    match = re.search(r"(?:на|за)\s+(\d+(?:[.,]\d+)?)", text)
    if match:
        return float(match.group(1).replace(",", "."))
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:доллар|тетер|usdt)", text)
    if match:
        return float(match.group(1).replace(",", "."))
    for word, value in sorted(_AMT_WORDS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?:на|за)\s+{word}(?![а-яa-z])", text):
            return float(value)
    return 0.0


class CryptoTradesMixin:
    def _execute_trade(self, text: str, kind: str, ticker: str | None) -> str:
        with common._TRADE_LOCK:
            return self._execute_trade_locked(text, kind, ticker)

    def _execute_trade_locked(self, text: str, kind: str, ticker: str | None) -> str:
        if not common._voice_trade_enabled():
            return "Сделки крипты выключены."
        amount = _extract_quote(text)
        if kind == "auto":
            self._earn_park_idle()
            return "Щуп и карман сами. Свободное сверх 12$ — в Earn."
        if not self._api_key:
            return "Без ключа Bybit заявки не выставляю."
        target = self._resolve_ticker(ticker)
        if kind == "allin":
            if not target:
                return "Куда перекладывать? Назови биткоин или эфир."
            return self._trade_all_in(target)
        if not target:
            return "Какую монету? Назови биткоин или эфир."
        if kind == "buy":
            return self._trade_buy(target, amount)
        return self._trade_sell(target, amount)

    def _resolve_ticker(self, ticker: str | None) -> str | None:
        if ticker:
            return ticker.upper()
        if len(self._watchlist) == 1:
            return self._watchlist[0]
        if self._last_tickers:
            return self._last_tickers[-1]
        return None

    def _cash_and_held(self) -> tuple[float, dict[str, dict[str, Any]]]:
        cash, positions = self._wallet()
        held = {item["ticker"]: item for item in positions}
        return cash, held

    def _trade_buy(self, ticker: str, amount: float | None) -> str:
        ticker = ticker.upper()
        cash, _held = self._cash_and_held()
        filters = self._filters(ticker)
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        if amount is None:
            quote = cash * common._BUY_CASH_BUFFER
        elif amount <= 0:
            quote = min_amt
        else:
            quote = amount
        quote = min(quote, cash * common._BUY_CASH_BUFFER)
        if quote + 1e-9 < min_amt:
            raise RuntimeError("no lots")
        return self._place_order(ticker, "Buy", quote_usdt=quote)

    def _trade_sell(self, ticker: str, amount: float | None) -> str:
        ticker = ticker.upper()
        _cash, held = self._cash_and_held()
        pos = held.get(ticker)
        if not pos:
            raise RuntimeError("no lots")
        price = float(pos["price"] or 0) or self._ticker(ticker)["price"]
        filters = self._filters(ticker)
        held_qty = float(pos["qty"])
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        step = filters["step"]
        if amount is None:
            qty = held_qty
        elif amount <= 0:
            need = min_amt / price if price else 0.0
            qty = max(filters["min_qty"], need)
            qty = float(_qty_str(qty, step, round_up=True) or 0)
        else:
            qty = amount / price if price else 0.0
            qty = float(_qty_str(qty, step) or 0)
        qty = min(qty, held_qty)
        leftover = (held_qty - qty) * price if price else 0.0
        if qty + 1e-12 < held_qty and leftover + 1e-9 < min_amt:
            qty = held_qty
        notional = qty * price if price else 0.0
        if qty <= 0 or (notional + 1e-9 < min_amt and qty + 1e-12 < held_qty):
            raise RuntimeError("no lots")
        return self._place_order(ticker, "Sell", base_qty=qty, price=price)

    def _trade_all_in(self, ticker: str) -> str:
        ticker = ticker.upper()
        parts: list[str] = []
        _cash, held = self._cash_and_held()
        for other, pos in list(held.items()):
            if other == ticker:
                continue
            if self._is_owner_position(other):
                continue
            parts.append(self._place_order(other, "Sell", base_qty=float(pos["qty"]), price=float(pos["price"])))
            time.sleep(0.4)
        time.sleep(0.3)
        self._bust_private_cache()
        return ( " ".join(parts) + " " if parts else "") + self._trade_buy(ticker, None)

    def _reset_trail_leg(self, ticker: str) -> None:
        """Новая покупка — трейл с нуля: старый пик/armed от прошлой позиции не наследуем."""
        legs = common.read_trail_state()
        if legs.pop(ticker.upper(), None) is not None:
            common.write_trail_state(legs)

    def _order_state(self, ticker: str, order_id: str) -> dict[str, Any]:
        """Статус заявки: сперва среди открытых, потом в истории."""
        for path in ("/v5/order/realtime", "/v5/order/history"):
            try:
                data = self._signed(
                    "GET",
                    path,
                    {"category": "spot", "symbol": self._symbol(ticker), "orderId": order_id},
                )
            except Exception:
                continue
            rows = (data.get("result") or {}).get("list") or []
            if rows:
                return rows[0]
        return {}

    def _open_spot_orders(self) -> list[dict[str, Any]]:
        """Открытые спот-заявки аккаунта (для reconcile кармана/рукава)."""
        try:
            data = self._signed(
                "GET",
                "/v5/order/realtime",
                {"category": "spot", "settleCoin": "USDT", "limit": 50},
            )
        except Exception as exc:
            logger.info("[Крипта] открытые заявки: %s", exc)
            return []
        rows = (data.get("result") or {}).get("list") or []
        return [row for row in rows if isinstance(row, dict)]

    def _cancel_order(self, ticker: str, order_id: str) -> None:
        try:
            self._signed(
                "POST",
                "/v5/order/cancel",
                {"category": "spot", "symbol": self._symbol(ticker), "orderId": order_id},
            )
        except Exception as exc:
            logger.info("[Крипта] отмена заявки %s: %s", ticker, exc)

    def _maker_leg(
        self,
        ticker: str,
        side: str,
        filters: dict[str, float],
        *,
        quote_usdt: float = 0.0,
        base_qty: float = 0.0,
    ) -> tuple[float, float, float, bool]:
        """Лимитная PostOnly у своей стороны книги.

        Возвращает (монеты, сумма, комиссия USDT, можно_добирать).
        Можно добирать только после терминального статуса — иначе риск оверфилла.
        """
        terminal = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        book = self._book(ticker)
        px = book["bid"] if side == "Buy" else book["ask"]
        if px <= 0:
            return 0.0, 0.0, 0.0, True
        price_s = common._price_str(px, filters["tick"], round_up=(side == "Sell"))
        px = float(price_s)
        qty = (quote_usdt / px if px else 0.0) if side == "Buy" else base_qty
        qty_s = _qty_str(qty, filters["step"])
        qty = float(qty_s or 0)
        if qty <= 0 or qty * px + 1e-9 < min_amt:
            return 0.0, 0.0, 0.0, True
        body = {
            "category": "spot",
            "symbol": self._symbol(ticker),
            "side": side,
            "orderType": "Limit",
            "timeInForce": "PostOnly",
            "isLeverage": 0,
            "qty": qty_s,
            "price": price_s,
        }
        try:
            data = self._signed("POST", "/v5/order/create", body)
        except Exception as exc:
            floor_px = _sell_price_floor_from_exc(exc) if side == "Sell" else None
            if floor_px is None:
                raise
            # PostOnly на полу выше ask не закроет выход — сразу к IOC/Market.
            logger.info(
                "[Крипта] лимитная Sell %s: пол биржи %.6g — пропускаю maker",
                ticker,
                floor_px,
            )
            return 0.0, 0.0, 0.0, True
        order_id = str((data.get("result") or {}).get("orderId") or "")
        if not order_id:
            return 0.0, 0.0, 0.0, True
        deadline = time.time() + common._MAKER_WAIT_SEC
        state: dict[str, Any] = {}
        status = ""
        while time.time() < deadline:
            time.sleep(common._MAKER_POLL_SEC)
            state = self._order_state(ticker, order_id)
            status = str(state.get("orderStatus") or "")
            if status in terminal:
                break
        if status not in terminal:
            ok, done_qty, done_quote, fee = self._cancel_limit_confirmed(
                ticker, order_id, side=side,
            )
            if done_qty > 0:
                logger.info(
                    "[Крипта] лимитная %s %s: %.6g на %.2f$",
                    side,
                    ticker,
                    done_qty,
                    done_quote,
                )
            if not ok:
                logger.info(
                    "[Крипта] лимитка %s %s ещё жива — добор не ставлю",
                    side,
                    ticker,
                )
            return done_qty, done_quote, fee, ok
        done_qty = float(state.get("cumExecQty") or 0)
        done_quote = float(state.get("cumExecValue") or 0)
        if done_qty > 0:
            logger.info(
                "[Крипта] лимитная %s %s: %.6g по %.6g на %.2f$",
                side,
                ticker,
                done_qty,
                px,
                done_quote,
            )
        return done_qty, done_quote, _cum_exec_fee_usdt(state, ticker, side), True

    def _await_fill(self, ticker: str, order_id: str, side: str = "") -> tuple[float, float, float]:
        """Сколько рыночная заявка реально исполнилась. Принятие ордера ещё не сделка."""
        deadline = time.time() + 8.0
        state: dict[str, Any] = {}
        while time.time() < deadline:
            state = self._order_state(ticker, order_id)
            status = str(state.get("orderStatus") or "")
            if status in {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}:
                break
            time.sleep(0.4)
        else:
            self._cancel_order(ticker, order_id)
            state = self._order_state(ticker, order_id)
        try:
            qty = float(state.get("cumExecQty") or 0)
            quote = float(state.get("cumExecValue") or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0, 0.0
        return max(qty, 0.0), max(quote, 0.0), _cum_exec_fee_usdt(state, ticker, side)

    def _rest_limit_sell(self, ticker: str, qty: float, price: float) -> str:
        """Лимитная продажа PostOnly, остаётся в стакане. Пустая строка — не встала."""
        ticker = ticker.upper()
        try:
            filters = self._filters(ticker)
            book = self._book(ticker)
            ask = float(book.get("ask") or 0)
            # Ниже аска PostOnly снимут: если ход уже выше тейка, встаём на аск.
            raw_px = max(float(price or 0), ask)
            if raw_px <= 0:
                return ""
            price_s = common._price_str(raw_px, filters["tick"], round_up=True)
            px = float(price_s)
            qty_s = _qty_str(qty, filters["step"])
            lot = float(qty_s or 0)
            if lot <= 0 or lot * px + 1e-9 < max(filters["min_amt"], _MIN_QUOTE):
                return ""
            data = self._signed(
                "POST",
                "/v5/order/create",
                {
                    "category": "spot",
                    "symbol": self._symbol(ticker),
                    "side": "Sell",
                    "orderType": "Limit",
                    "timeInForce": "PostOnly",
                    "isLeverage": 0,
                    "qty": qty_s,
                    "price": price_s,
                },
            )
            order_id = str((data.get("result") or {}).get("orderId") or "")
            if order_id:
                logger.info("[Крипта] лимитный тейк %s: %.6g по %s", ticker, lot, price_s)
            return order_id
        except Exception as exc:
            logger.info("[Крипта] лимитный тейк %s не встал: %s", ticker, exc)
            return ""

    def _cancel_limit_confirmed(
        self,
        ticker: str,
        order_id: str,
        *,
        side: str = "Sell",
    ) -> tuple[bool, float, float, float]:
        """Снять лимитку и дождаться конечного статуса.

        False — заявка ещё жива, добор ставить нельзя.
        Возвращает (подтверждено, исполнено монет, сумма, комиссия).
        """
        terminal = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
        ticker = ticker.upper()
        state = self._order_state(ticker, order_id)
        status = str(state.get("orderStatus") or "")
        if status not in terminal:
            self._cancel_order(ticker, order_id)
            deadline = time.time() + common._IOC_CANCEL_WAIT_SEC
            while time.time() < deadline:
                time.sleep(0.3)
                state = self._order_state(ticker, order_id)
                status = str(state.get("orderStatus") or "")
                if status in terminal:
                    break
        try:
            done_qty = max(0.0, float(state.get("cumExecQty") or 0))
            done_quote = max(0.0, float(state.get("cumExecValue") or 0))
        except (TypeError, ValueError):
            done_qty = done_quote = 0.0
        fee = _cum_exec_fee_usdt(state, ticker, side)
        if status not in terminal:
            # Заявка ещё жива — добор запрещён; уже известный fill всё же возвращаем.
            return False, done_qty, done_quote, fee
        return True, done_qty, done_quote, fee

    def _ioc_leg(
        self,
        ticker: str,
        side: str,
        filters: dict[str, float],
        *,
        quote_usdt: float = 0.0,
        base_qty: float = 0.0,
    ) -> tuple[float, float, float]:
        """Добор Limit IOC с потолком ask/bid ± slip. Без слепого Market."""
        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        book = self._book(ticker)
        ask = float(book.get("ask") or 0)
        bid = float(book.get("bid") or 0)
        slip = common._IOC_MAX_SLIP_PCT / 100.0
        if side == "Buy":
            if ask <= 0 or quote_usdt <= 0:
                return 0.0, 0.0, 0.0
            cap = ask * (1.0 + slip)
            price_s = common._price_str(cap, filters["tick"], round_up=True)
            px = float(price_s)
            if px <= 0:
                return 0.0, 0.0, 0.0
            qty_s = _qty_str(quote_usdt / px, filters["step"])
            qty = float(qty_s or 0)
            if qty <= 0 or qty * px + 1e-9 < min_amt:
                return 0.0, 0.0, 0.0
        else:
            if bid <= 0 or base_qty <= 0:
                return 0.0, 0.0, 0.0
            # Sell IOC только по лучшему bid: bid−slip на тонкой книге даёт
            # EC_ReachMarketPriceLimit (INIT 202 шт. сносили стакан).
            last = float(book.get("last") or 0)
            floor = bid
            if last > 0:
                floor = max(floor, last * (1.0 - slip))
            # Не ниже bid: иначе снова уходим вглубь книги.
            floor = max(floor, bid)
            price_s = common._price_str(floor, filters["tick"], round_up=True)
            px = float(price_s)
            qty_s = _qty_str(base_qty, filters["step"])
            qty = float(qty_s or 0)
            if px <= 0 or qty <= 0 or qty * px + 1e-9 < min_amt:
                return 0.0, 0.0, 0.0
        body = {
            "category": "spot",
            "symbol": self._symbol(ticker),
            "side": side,
            "orderType": "Limit",
            "timeInForce": "IOC",
            "isLeverage": 0,
            "qty": qty_s,
            "price": price_s,
        }
        try:
            data = self._signed("POST", "/v5/order/create", body)
        except Exception as exc:
            # Sell ниже пола: если пол ≤ bid — IOC на полу; если пол выше bid —
            # лимит не скрестит книгу, пусть _place_order бьёт Market.
            floor_px = _sell_price_floor_from_exc(exc) if side == "Sell" else None
            if floor_px is None:
                raise
            if floor_px > bid + 1e-12:
                # Биржа запрещает продажу ниже пола, а bid уже ниже — только лимитка на полу.
                raise RuntimeError(
                    f"пол биржи {floor_px:g} > bid {bid:g}"
                )
            if floor_px <= px + 1e-12:
                raise
            price_s = common._price_str(floor_px, filters["tick"], round_up=True)
            px = float(price_s)
            body = {**body, "price": price_s}
            logger.info(
                "[Крипта] IOC Sell %s: пол биржи %.6g — ставлю %s",
                ticker,
                floor_px,
                price_s,
            )
            data = self._signed("POST", "/v5/order/create", body)
        order_id = str((data.get("result") or {}).get("orderId") or "")
        if not order_id:
            return 0.0, 0.0, 0.0
        done_qty, done_quote, fee = self._await_fill(ticker, order_id, side)
        if done_qty > 0 and done_quote <= 0:
            done_quote = done_qty * px
        if done_qty > 0:
            logger.info(
                "[Крипта] IOC %s %s: %.6g по %.6g на %.2f$ (потолок %s)",
                side,
                ticker,
                done_qty,
                done_quote / done_qty,
                done_quote,
                price_s,
            )
        return done_qty, done_quote, fee

    def _place_order(
        self,
        ticker: str,
        side: str,
        quote_usdt: float | None = None,
        base_qty: float | None = None,
        price: float = 0.0,
        maker: bool = False,
    ) -> str:
        ticker = ticker.upper()
        filters = self._filters(ticker)
        px = float(price or 0) or self._ticker(ticker)["price"]
        want_quote = float(quote_usdt or 0)
        want_qty = float(base_qty or 0)
        if side == "Buy":
            if want_quote <= 0:
                raise RuntimeError("no lots")
        else:
            if float(_qty_str(want_qty, filters["step"]) or 0) <= 0:
                raise RuntimeError("no lots")
            want_quote = want_qty * px

        if side == "Buy":
            self._earn_free_cash(want_quote)
        maker_qty = maker_quote = maker_fee = 0.0
        can_top_up = True
        use_maker = bool(maker and common._maker_orders_enabled())
        if use_maker:
            try:
                maker_qty, maker_quote, maker_fee, can_top_up = self._maker_leg(
                    ticker,
                    side,
                    filters,
                    quote_usdt=want_quote,
                    base_qty=want_qty,
                )
            except Exception as exc:
                logger.info("[Крипта] лимитная %s %s не прошла: %s", side, ticker, exc)

        min_amt = max(filters["min_amt"], _MIN_QUOTE)
        rem_quote = want_quote - maker_quote if side == "Buy" else 0.0
        rem_qty = max(0.0, want_qty - maker_qty) if side == "Sell" else 0.0
        rem_notional = rem_quote if side == "Buy" else rem_qty * px
        # Остаток меньше минимального лота — лимитной хватило.
        if rem_notional + 1e-9 < min_amt:
            if maker_quote <= 0 and maker_qty <= 0:
                raise RuntimeError("no lots")
            fill_quote = maker_quote if maker_quote > 0 else maker_qty * px
            fill_qty = maker_qty
            return self._finish_order(
                ticker,
                side,
                fill_quote,
                fill_quote / fill_qty if fill_qty else px,
                qty=fill_qty,
                fee_usd=maker_fee,
            )
        if use_maker and not can_top_up:
            if maker_qty <= 0:
                raise RuntimeError("заявка не исполнилась")
            fill_quote = maker_quote if maker_quote > 0 else maker_qty * px
            return self._finish_order(
                ticker,
                side,
                fill_quote,
                fill_quote / maker_qty,
                qty=maker_qty,
                fee_usd=maker_fee,
            )

        top_qty = top_quote = top_fee = 0.0
        if use_maker:
            try:
                top_qty, top_quote, top_fee = self._ioc_leg(
                    ticker,
                    side,
                    filters,
                    quote_usdt=rem_quote,
                    base_qty=rem_qty,
                )
            except Exception as exc:
                logger.info("[Крипта] IOC %s %s не прошёл: %s", side, ticker, exc)
        else:
            body: dict[str, Any] = {
                "category": "spot",
                "symbol": self._symbol(ticker),
                "side": side,
                "orderType": "Market",
                "isLeverage": 0,
            }
            if side == "Buy":
                body["marketUnit"] = "quoteCoin"
                body["qty"] = f"{rem_quote:.2f}"
            else:
                body["marketUnit"] = "baseCoin"
                body["qty"] = _qty_str(rem_qty, filters["step"])
            data = self._signed("POST", "/v5/order/create", body)
            result = data.get("result") or {}
            order_id = str(result.get("orderId") or "")
            if not order_id and not result.get("orderLinkId"):
                raise RuntimeError(str(data.get("retMsg") or "заявка не прошла"))
            top_qty, top_quote, top_fee = (
                self._await_fill(ticker, order_id, side) if order_id else (0.0, 0.0, 0.0)
            )
            if top_qty > 0 and top_quote <= 0:
                top_quote = top_qty * px
            if top_qty > 0:
                logger.info(
                    "[Крипта] рыночная %s %s: %.6g по %.6g на %.2f$",
                    side,
                    ticker,
                    top_qty,
                    top_quote / top_qty,
                    top_quote,
                )

        total_qty = maker_qty + top_qty
        total_quote = maker_quote + top_quote
        # Крупный Market Sell → EC_ReachMarketPriceLimit. Режем IOC@bid по уровням.
        floor_block = ""
        if side == "Sell":
            for _pass in range(8):
                left = max(0.0, want_qty - total_qty)
                if left * max(px, 1e-12) + 1e-9 < min_amt:
                    break
                # Первый IOC уже сделан в use_maker-ветке — не дублируем зря.
                if use_maker and _pass == 0 and top_qty > 0:
                    continue
                try:
                    s_qty, s_quote, s_fee = self._ioc_leg(
                        ticker, "Sell", filters, base_qty=left,
                    )
                except Exception as exc:
                    msg = str(exc or "")
                    logger.info("[Крипта] IOC Sell %s добор: %s", ticker, exc)
                    if "пол биржи" in msg.lower() or _sell_price_floor_from_exc(msg):
                        floor_block = msg
                    break
                if s_qty <= 0:
                    break
                total_qty += s_qty
                total_quote += s_quote
                top_fee += s_fee
                if _pass > 0 or not use_maker:
                    logger.info(
                        "[Крипта] IOC Sell %s уровень: %.6g на %.2f$",
                        ticker,
                        s_qty,
                        s_quote,
                    )
        if total_qty <= 0 or total_quote <= 0:
            if floor_block:
                raise RuntimeError(floor_block)
            raise RuntimeError("заявка не исполнилась")
        return self._finish_order(
            ticker,
            side,
            total_quote,
            total_quote / total_qty,
            qty=total_qty,
            fee_usd=maker_fee + top_fee,
        )

    def _finish_order(
        self,
        ticker: str,
        side: str,
        filled_quote: float,
        px: float,
        *,
        qty: float = 0.0,
        fee_usd: float = 0.0,
    ) -> str:
        if qty <= 0 and px > 0:
            qty = filled_quote / px
        self._last_fill = {
            "qty": float(qty),
            "quote": float(filled_quote),
            "price": float(px),
            "fee": float(fee_usd or 0),
        }
        self._bust_private_cache()
        if side == "Buy":
            self._mark_desk_bought(ticker)
            self._reset_trail_leg(ticker)
        spoken = _spoken(ticker)
        verb = "Купил" if side == "Buy" else "Продал"
        phrase = f"{verb} {spoken} на {_format_usd(filled_quote)}."
        self._journal_trade(ticker, side, filled_quote, px, spoken, fee_usd=fee_usd)
        self._last_tickers = [ticker]
        return phrase
