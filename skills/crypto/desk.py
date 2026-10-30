# skills/crypto/desk.py
# Автостол: только щуп и карман (SOL + ноги с handoff щупа).

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import common
from .common import (
    _ADVICE_HINTS,
    _DESK_PERIOD_SEC,
    _MIN_QUOTE,
    _read_ticker_set,
    _write_ticker_set,
    read_anchors,
    read_blacklist,
    read_btc_dip,
    read_entry_arms,
    probe_legs,
    probe_arms,
    probe_tickers,
    read_probe,
    read_second_pocket_legs,
    drop_second_pocket_leg,
    second_pocket_qty,
    second_pocket_tickers,
    update_second_pocket_leg,
    upsert_second_pocket_leg,
    write_second_pocket_legs,
    write_anchors,
    write_btc_dip,
    write_entry_arms,
    write_probe,
)
from .desk_policy import (
    PROBE_ENTRY_HOLD_SEC,
    probe_period_sec,
    probe_scan_algo_order,
    PROBE_ARMED_PERIOD_SEC,
    PROBE_SCAN_LIMIT,
    PROBE_SCAN_CLASSIC_LIMIT,
    PROBE_ALGO_ORDER,
    set_live_sleeve,
    SLEEVE_SKIP,
    _leveraged_base,
    BTC_DIP_TICKER,
    POCKET_EXIT_DEEP_PULLBACK_PCT,
    btc_dip_quote,
    probe_algo_params,
    probe_arm_action,
    probe_exit_reason,
    probe_candle_buy_ok,
    probe_grind_live_ok,
    probe_should_handoff,
    PROBE_ALGO_CANDLE,
    PROBE_ALGO_GRIND,
    PROBE_MAX_LEGS,
    PROBE_REENTRY_HOURS,
    PROBE_REENTRY_BREAKOUT_PCT,
    pocket_from_probe_max,
    probe_pick_setup,
    probe_quote,
    probe_quote_for_buy,
    PROBE_BOOST_USD,
    probe_scan_rank,
    probe_scan_ranked_rows,
    probe_trends_ok,
    pocket_dip_held,
    pocket_dip_pending,
    pocket_swing,
    update_pocket_swing_arm,
    POCKET_SWING_BARS,
    ma_trend,
    position_basis,
    btc_dip_exit_reason,
    update_pocket_exit_arm,
    exit_trap_arm_pct,
    pocket_bounce_ready,
    pocket_entry_trends_ok,
    cfg,
    ALT_HELD_MIN_USD,
    MANUAL_TRAP_SKIP,
    MANUAL_TRAP_MAX_LEGS,
    manual_trap_free_qty,
)
from . import journal as crypto_journal
from .reconcile import (
    ORPHAN_ORDER_AGE_SEC,
    orphan_open_orders,
    reconcile_pocket_qty,
    reconcile_second_pocket_legs,
    wallet_qty_map,
    wallet_value_map,
)

logger = logging.getLogger(__name__)

_RECONCILE_MIN_GAP_SEC = 45.0


def _wants_advice(text: str) -> bool:
    return any(hint in text for hint in _ADVICE_HINTS)


def _exit_trigger(arm: dict[str, float]) -> float:
    """Цена, с которой откат уже считается сработавшим. Выше — ближе к продаже."""
    high = float(arm.get("high") or 0)
    pullback = float(arm.get("pullback") or 0)
    return high * (1.0 - pullback / 100.0)


def _cycle_error_kind(exc: BaseException) -> str:
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "502" in text or "503" in text or "504" in text:
        return "bybit 5xx"
    if "bad json" in text or "json" in text:
        return "json"
    if "transient" in text:
        return "сеть"
    return "ошибка"


class CryptoDeskMixin:
    def _ensure_desk_loop(self) -> None:
        with common._desk_init_lock:
            if common._desk_loop_started:
                return
            self._desk_thread = threading.Thread(
                target=self._desk_loop,
                name="crypto-desk",
                daemon=True,
            )
            self._desk_thread.start()
            common._desk_loop_started = True
            try:
                from crypto_probe_watch import start_probe_watch_thread

                start_probe_watch_thread(notify_on_start=False)
            except Exception:
                pass

    def on_disabled(self) -> None:
        self._desk_enabled.clear()

    def on_enabled(self) -> None:
        self._desk_enabled.set()
        self.start_background()

    def start_background(self) -> None:
        self._ensure_desk_loop()

    def _run_advisor(self, channel: str = "voice") -> str:
        import crypto_advisor

        send_tg = common.telegram_configured() and channel != "telegram"
        text = crypto_advisor.run(skill=self, to_telegram=send_tg, to_stdout=False)
        if channel == "telegram":
            return text
        if common.telegram_configured():
            return "Отправил совет по крипте в телеграм."
        return text

    def _bust_private_cache(self) -> None:
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if not key.startswith("px:")
        }

    def _mark_desk_bought(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._desk_bought.add(ticker)
        self._desk_bought_ready = True
        _write_ticker_set(common._BOUGHT_PATH, self._desk_bought)

    def _ensure_desk_bought(self, held: set[str]) -> None:
        """Если bought.json нет — не зачисляем держанное в стол (иначе продадим ручное)."""
        if self._desk_bought_ready:
            return
        # Пустой список стола: всё на балансе считается чужим, пока стол сам не купит.
        self._desk_bought = set()
        self._desk_bought_ready = True
        _write_ticker_set(common._BOUGHT_PATH, self._desk_bought)
        if held:
            logger.info(
                "[Крипта] нет bought.json — %d позиций на балансе не трогаю как чужие",
                len(held),
            )

    def _is_owner_position(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker in self._manual_holds:
            return True
        return self._desk_bought_ready and ticker not in self._desk_bought

    def _refresh_sleeve(self) -> None:
        """Рукав и alloc сняты — только щуп и карман."""
        set_live_sleeve(set())

    def desk_candidates(self) -> list[dict[str, Any]]:
        """Лента для советника (без автопортфеля)."""
        try:
            return list(self._tape()[:40])
        except Exception:
            return []

    def desk_auto_candidates(self) -> list[dict[str, Any]]:
        return []

    def desk_score_alloc(
        self, candidates: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, float], str]:
        return {}, "Автостол: только щуп и карман."

    def ai_pick_alloc(self, candidates: list[dict[str, Any]]) -> tuple[dict[str, float], str]:
        return {}, "Портфельный alloc снят. Работают щуп и карман."

    def _trade_auto(self, silent: bool = False) -> str:
        with common._TRADE_LOCK:
            try:
                self._reconcile_desk_state()
                return self._trade_auto_locked(silent)
            finally:
                self._earn_park_idle()

    def _trade_auto_locked(self, silent: bool = False) -> str:
        msg = "Авторебаланс снят. В фоне: щуп и карман."
        if silent:
            logger.info("[Крипта] авто: %s", msg)
        return msg

    def _desk_watch(self, silent: bool = True) -> str:
        return "Дозор рукава снят."

    def _desk_watch_locked(self, silent: bool = True) -> str:
        return "Дозор рукава снят."

    def _rebalance(self, target_alloc: dict[str, float], *, respect_hold: bool = True) -> str:
        return "Ребаланс снят."

    def _reconcile_desk_state(self, *, force: bool = False) -> list[str]:
        """Сверить карман/рукав с биржей. Щуп не читаем на запись и не отменяем его заявки."""
        if not self._api_key:
            return []
        now = time.time()
        last = float(getattr(self, "_reconcile_ts", 0) or 0)
        if not force and last > 0 and now - last < _RECONCILE_MIN_GAP_SEC:
            return []
        self._reconcile_ts = now
        notes: list[str] = []
        try:
            _cash, positions = self._wallet(with_earn=False)
        except Exception as exc:
            logger.warning("[Крипта] reconcile кошелёк: %s", exc)
            return notes
        qtys = wallet_qty_map(positions)
        values = wallet_value_map(positions)
        prices = {
            str(item.get("ticker") or "").upper(): float(item.get("price") or 0)
            for item in positions
            if item.get("ticker")
        }
        # Карман SOL — qty файла не больше живого баланса.
        dip = read_btc_dip()
        local_qty = float(dip.get("qty") or 0)
        if local_qty > 0:
            fixed = reconcile_pocket_qty(
                local_qty=local_qty,
                wallet_qty=float(qtys.get(BTC_DIP_TICKER) or 0),
                price=float(prices.get(BTC_DIP_TICKER) or 0),
            )
            if abs(fixed - local_qty) > 1e-12:
                if fixed <= 0:
                    write_btc_dip(0, 0)
                    self._drop_anchor(BTC_DIP_TICKER)
                    notes.append("SOL сброшен (нет на балансе)")
                else:
                    write_btc_dip(
                        fixed,
                        float(dip.get("entry") or 0),
                        exit_high=float(dip.get("exit_high") or 0),
                        exit_ts=float(dip.get("exit_ts") or 0),
                        low=float(dip.get("low") or 0),
                    )
                    notes.append(f"SOL qty {local_qty:.6g}→{fixed:.6g}")

        # Второй карман (ноги с щупа) — по тикеру.
        before_second = read_second_pocket_legs()
        cleaned_second, second_notes = reconcile_second_pocket_legs(
            before_second,
            wallet_qtys=qtys,
            prices=prices,
        )
        if cleaned_second != before_second:
            write_second_pocket_legs(cleaned_second)
            for coin in {str(leg["ticker"]) for leg in before_second} - {
                str(leg["ticker"]) for leg in cleaned_second
            }:
                self._drop_anchor(coin)
        notes.extend(second_notes)

        probe_skip = set(probe_tickers()) | second_pocket_tickers()

        # Зависшие лимитки по карманам и щупу.
        try:
            open_rows = self._open_spot_orders()
        except Exception as exc:
            logger.info("[Крипта] reconcile заявки: %s", exc)
            open_rows = []
        managed = {BTC_DIP_TICKER} | second_pocket_tickers() | probe_skip
        orphans = orphan_open_orders(
            open_rows,
            now=now,
            skip_tickers=probe_skip,
            only_tickers=managed,
            max_age_sec=ORPHAN_ORDER_AGE_SEC,
        )
        for item in orphans:
            self._cancel_order(item["ticker"], item["order_id"])
            notes.append(f"отмена {item['ticker']}")
        if notes:
            logger.info("[Крипта] reconcile: %s", "; ".join(notes))
        return notes

    def _ensure_tape(self) -> None:
        tape = getattr(self, "_bybit_tape", None)
        if tape is not None:
            return
        from .stream import BybitPublicTape

        tape = BybitPublicTape()
        self._bybit_tape = tape
        seed = {BTC_DIP_TICKER} | second_pocket_tickers()
        probe = read_probe()
        seed |= probe_tickers(probe)
        for arm in probe_arms(probe):
            seed.add(str(arm["ticker"]))
        tape.set_symbols(seed)
        tape.start()

    def _sync_tape_symbols(self) -> None:
        """Лента: символы щупа и карманов."""
        tape = getattr(self, "_bybit_tape", None)
        if tape is None:
            return
        symbols = {BTC_DIP_TICKER} | second_pocket_tickers()
        probe = read_probe()
        symbols |= probe_tickers(probe)
        for arm in probe_arms(probe):
            symbols.add(str(arm["ticker"]))
        for leg in probe_legs(probe):
            symbols.add(str(leg.get("ticker") or ""))
        arms = read_entry_arms()
        triggers: dict[str, dict[str, float]] = {}
        pocket_arm = arms.get("pocket")
        if pocket_arm:
            triggers[BTC_DIP_TICKER] = {
                "low": float(pocket_arm["low"]),
                "bounce": cfg().pocket_bounce_pct,
                "hold": float(cfg().pocket_hold_sec),
                "ts": float(pocket_arm.get("ts") or 0),
            }
        exits: dict[str, dict[str, float]] = {}
        dip = read_btc_dip()
        exit_high = float(dip.get("exit_high") or 0)
        if float(dip.get("qty") or 0) > 0 and exit_high > 0:
            exits[BTC_DIP_TICKER] = {
                "high": exit_high,
                "pullback": cfg().pocket_exit_pullback_pct,
                "deep_pullback": POCKET_EXIT_DEEP_PULLBACK_PCT,
                "hold": float(cfg().pocket_exit_hold_sec),
                "ts": float(dip.get("exit_ts") or 0),
            }
        for pocket_leg in read_second_pocket_legs():
            coin = str(pocket_leg["ticker"])
            high = float(pocket_leg.get("exit_high") or 0)
            if high <= 0:
                continue
            symbols.add(coin)
            row = {
                "high": high,
                "pullback": cfg().pocket_exit_pullback_pct,
                "deep_pullback": POCKET_EXIT_DEEP_PULLBACK_PCT,
                "hold": float(cfg().pocket_exit_hold_sec),
                "ts": float(pocket_leg.get("exit_ts") or 0),
            }
            prev = exits.get(coin)
            if prev is None or _exit_trigger(row) >= _exit_trigger(prev):
                exits[coin] = row
        tape.set_symbols({s for s in symbols if s})
        tape.set_entry_arms(triggers)
        tape.set_exit_arms(exits)

    def _open_pocket_qty(self, ticker: str) -> float:
        qty = 0.0
        if ticker == BTC_DIP_TICKER:
            qty += float(read_btc_dip().get("qty") or 0)
        qty += second_pocket_qty(ticker)
        for leg in probe_legs():
            if ticker == str(leg.get("ticker") or ""):
                qty += float(leg.get("qty") or 0)
        return qty

    def _second_ticker(self) -> str | None:
        """Устарело: второй карман — мульти-ноги с щупа. Для старых вызовов — первая нога."""
        legs = read_second_pocket_legs()
        if legs:
            return str(legs[0]["ticker"])
        return None

    def _drop_anchor(self, ticker: str) -> None:
        anchors = read_anchors()
        if ticker not in anchors:
            return
        anchors.pop(ticker, None)
        write_anchors(anchors)

    def _prune_anchors(self, held: set[str]) -> None:
        anchors = read_anchors()
        kept = {ticker: price for ticker, price in anchors.items() if ticker in held}
        if len(kept) != len(anchors):
            write_anchors(kept)

    def _tape_quote(self, ticker: str) -> dict[str, float] | None:
        tape = getattr(self, "_bybit_tape", None)
        if tape is None:
            return None
        return tape.quote(ticker)

    def _pocket_swing_low(self, ticker: str, day_chg: float | None) -> tuple[bool, float | None]:
        """(False, None) — свечей нет, дно не трогаем. (True, low|None) — откат известен."""
        del day_chg
        try:
            bars = self._probe_bars(ticker, "15", POCKET_SWING_BARS + 2)
        except Exception as exc:
            logger.info("[Крипта] карман %s свечи: %s", ticker, exc)
            return False, None
        if len(bars) < 4:
            return False, None
        return True, pocket_swing(bars)

    def _pocket_dip_held(self, ticker: str, low: float) -> bool:
        try:
            bars = self._probe_bars(ticker, "15", POCKET_SWING_BARS + 2)
        except Exception as exc:
            logger.info("[Крипта] карман %s свечи: %s", ticker, exc)
            return False
        return pocket_dip_held(bars, low)

    def _swing_pocket_arm(
        self,
        ticker: str,
        state: dict[str, Any] | None,
        *,
        price: float,
        day_chg: float | None,
        now: float,
        taped: dict[str, float],
    ) -> dict[str, float] | None:
        known, swing_low = self._pocket_swing_low(ticker, day_chg)
        if not known:
            return state
        if state and taped.get(ticker):
            taped_low = float(taped[ticker])
            if taped_low < float(state["low"]) - 1e-12:
                state = {**state, "low": taped_low, "ts": now}
        keep = False
        if state and float(state.get("low") or 0) > 0:
            try:
                bars = self._probe_bars(ticker, "15", POCKET_SWING_BARS + 2)
                keep = pocket_dip_pending(bars, float(state["low"]))
            except Exception:
                keep = False
        updated = update_pocket_swing_arm(
            state, price=price, swing_low=swing_low, now=now, keep=keep
        )
        fresh = state is None or (
            updated is not None and float(updated["low"]) < float(state.get("low") or 0) - 1e-12
        )
        if updated and fresh:
            logger.info(
                "[Крипта] карман %s откат от верха — жду отскок %.1f%% от %.4g",
                ticker,
                cfg().pocket_bounce_pct,
                updated["low"],
            )
        return updated

    def _trade_second_pocket_legs(
        self,
        *,
        positions: dict[str, dict[str, Any]],
        prices: dict[str, float],
        blocked: set[str],
    ) -> list[str]:
        """Выход по ногам второго кармана (переданы с щупа). Покупок здесь нет."""
        phrases: list[str] = []
        for leg in list(read_second_pocket_legs()):
            ticker = str(leg.get("ticker") or "")
            if not ticker or ticker in blocked:
                continue
            qty = float(leg.get("qty") or 0)
            book_entry = float(leg.get("entry") or 0)
            if qty <= 0 or book_entry <= 0:
                continue
            price = float(prices.get(ticker) or 0)
            live = self._tape_quote(ticker)
            if live and float(live.get("price") or 0) > 0:
                price = float(live["price"])
            entry = position_basis(book_entry, read_anchors().get(ticker))
            now = time.time()
            exit_state = None
            if float(leg.get("exit_high") or 0) > 0:
                exit_state = {
                    "high": float(leg["exit_high"]),
                    "ts": float(leg.get("exit_ts") or 0),
                }
            tape = getattr(self, "_bybit_tape", None)
            taped_high = float((tape.exit_highs() if tape is not None else {}).get(ticker) or 0)
            if exit_state and taped_high > float(exit_state["high"]) + 1e-12:
                exit_state = {"high": taped_high, "ts": now}
            exit_state = update_pocket_exit_arm(
                exit_state, price=price, entry=entry, now=now, qty=qty
            )
            if exit_state:
                update_second_pocket_leg(
                    ticker,
                    qty=qty,
                    entry=book_entry,
                    low=float(leg.get("low") or 0),
                    exit_high=float(exit_state["high"]),
                    exit_ts=float(exit_state["ts"]),
                )
            reason = btc_dip_exit_reason(
                price=price,
                entry=entry,
                exit_arm=exit_state,
                now=now,
                swing_low=float(leg.get("low") or 0),
            )
            if not reason or price <= 0:
                if exit_state and price > 0:
                    need = float(exit_state["high"]) * (
                        1.0 - cfg().pocket_exit_pullback_pct / 100.0
                    )
                    logger.info(
                        "[Крипта] карман2 %s жду откат: верх %.4g сейчас %.4g нужно %.4g",
                        ticker,
                        float(exit_state["high"]),
                        price,
                        need,
                    )
                continue
            held = float((positions.get(ticker) or {}).get("qty") or 0)
            sell_qty = min(qty, held)
            if sell_qty <= 0:
                drop_second_pocket_leg(ticker)
                self._drop_anchor(ticker)
                continue
            seen = getattr(self, "_last_fill", None)
            try:
                phrase = self._place_order(
                    ticker, "Sell", base_qty=sell_qty, price=price, maker=True
                )
            except Exception as exc:
                logger.info("[Крипта] карман2 %s продажа не прошла: %s", ticker, exc)
                continue
            fill = getattr(self, "_last_fill", None)
            sold = (
                float(fill.get("qty") or 0)
                if fill is not seen and isinstance(fill, dict)
                else sell_qty
            )
            remain = max(0.0, qty - sold)
            if remain * price < _MIN_QUOTE:
                drop_second_pocket_leg(ticker)
                self._drop_anchor(ticker)
            else:
                update_second_pocket_leg(
                    ticker,
                    qty=remain,
                    entry=book_entry,
                    low=float(leg.get("low") or 0),
                    exit_high=float(leg.get("exit_high") or 0),
                    exit_ts=float(leg.get("exit_ts") or 0),
                )
                logger.info(
                    "[Крипта] карман2 %s ещё %.6g монет после неполной продажи",
                    ticker,
                    remain,
                )
            fill_px = float(fill.get("price") or 0) if isinstance(fill, dict) else 0.0
            logger.info(
                "[Крипта] карман2 %s: выход (%s) %.6g по %.4g",
                ticker,
                reason,
                sold,
                fill_px or price,
            )
            phrases.append(phrase)
        return phrases

    def _trade_btc_dip(
        self,
        *,
        day_chg: float | None,
        positions: dict[str, dict[str, Any]],
        price: float,
        blocked: bool,
        equity: float = 0.0,
        allow_entry: bool = True,
        ticker: str | None = None,
    ) -> str | None:
        """Карман SOL на долю счёта. Уже открытый не добираем."""
        del day_chg
        ticker = ticker or BTC_DIP_TICKER
        if ticker != BTC_DIP_TICKER:
            # AVA/SUI больше не покупаем во второй карман — только handoff с щупа.
            return None
        if ticker in read_blacklist():
            allow_entry = False
        dip = read_btc_dip()
        qty = float(dip.get("qty") or 0)
        if qty > 0:
            book_entry = float(dip.get("entry") or 0)
            entry = position_basis(book_entry, read_anchors().get(ticker))
            live = self._tape_quote(ticker)
            if live and float(live.get("price") or 0) > 0:
                price = float(live["price"])
            now = time.time()
            exit_state = None
            if float(dip.get("exit_high") or 0) > 0:
                exit_state = {"high": float(dip["exit_high"]), "ts": float(dip.get("exit_ts") or 0)}
            tape = getattr(self, "_bybit_tape", None)
            taped_high = float((tape.exit_highs() if tape is not None else {}).get(ticker) or 0)
            if exit_state and taped_high > float(exit_state["high"]) + 1e-12:
                exit_state = {"high": taped_high, "ts": now}
            exit_state = update_pocket_exit_arm(
                exit_state, price=price, entry=entry, now=now, qty=qty
            )
            if exit_state:
                write_btc_dip(
                    qty,
                    book_entry,
                    exit_high=float(exit_state["high"]),
                    exit_ts=float(exit_state["ts"]),
                    low=float(dip.get("low") or 0),
                )
            reason = btc_dip_exit_reason(
                price=price,
                entry=entry,
                exit_arm=exit_state,
                now=now,
                swing_low=float(dip.get("low") or 0),
            )
            if not reason or price <= 0:
                if exit_state and price > 0:
                    need = float(exit_state["high"]) * (1.0 - cfg().pocket_exit_pullback_pct / 100.0)
                    logger.info(
                        "[Крипта] карман %s жду откат: верх %.4g сейчас %.4g нужно %.4g",
                        ticker,
                        float(exit_state["high"]),
                        price,
                        need,
                    )
                return None
            held = float((positions.get(ticker) or {}).get("qty") or 0)
            sell_qty = min(qty, held)
            if sell_qty <= 0:
                write_btc_dip(0, 0)
                self._drop_anchor(ticker)
                return None
            seen = getattr(self, "_last_fill", None)
            try:
                phrase = self._place_order(ticker, "Sell", base_qty=sell_qty, price=price, maker=True)
            except Exception as exc:
                logger.info("[Крипта] карман %s продажа не прошла: %s", ticker, exc)
                return None
            fill = getattr(self, "_last_fill", None)
            sold = float(fill.get("qty") or 0) if fill is not seen and isinstance(fill, dict) else sell_qty
            remain = max(0.0, qty - sold)
            if remain * price < _MIN_QUOTE:
                write_btc_dip(0, 0)
                self._drop_anchor(ticker)
            else:
                write_btc_dip(
                    remain,
                    book_entry,
                    exit_high=float(dip.get("exit_high") or 0),
                    exit_ts=float(dip.get("exit_ts") or 0),
                    low=float(dip.get("low") or 0),
                )
                logger.info("[Крипта] карман %s ещё %.6g монет после неполной продажи", ticker, remain)
            fill_px = float(fill.get("price") or 0) if isinstance(fill, dict) else 0.0
            logger.info(
                "[Крипта] карман %s: выход (%s) %.6g по %.4g",
                ticker,
                reason,
                sold,
                fill_px or price,
            )
            return phrase
        if blocked or not allow_entry:
            return None
        arms_now = read_entry_arms()
        arm = arms_now.get("pocket")
        if not pocket_bounce_ready(arm, price=price, now=time.time()):
            return None
        if not self._pocket_dip_held(ticker, float(arm["low"])):
            logger.info(
                "[Крипта] карман %s жду закрытую свечу после дна %.4g",
                ticker,
                float(arm["low"]),
            )
            return None
        try:
            bars15 = self._probe_bars(ticker, "15", 80)
            bars1h = self._probe_bars(ticker, "60", 80)
            trend15 = ma_trend([bar["close"] for bar in bars15]) if bars15 else "neutral"
            trend1h = ma_trend([bar["close"] for bar in bars1h]) if bars1h else "neutral"
        except Exception as exc:
            logger.info("[Крипта] карман %s тренд: %s", ticker, exc)
            return None
        week = self._probe_week(ticker)
        if not pocket_entry_trends_ok(trend15=trend15, trend1h=trend1h, week_chg=week):
            logger.info(
                "[Крипта] карман %s пропуск входа: 15m %s 1h %s 7d %s",
                ticker,
                trend15,
                trend1h,
                f"{week:+.1f}%" if week is not None else "нет",
            )
            return None
        budget = btc_dip_quote(equity)
        free_cash, _held_now = self._cash_and_held()
        quote = min(budget, free_cash * common._BUY_CASH_BUFFER)
        if budget < _MIN_QUOTE or quote < budget * 0.9:
            return None
        before = float((positions.get(ticker) or {}).get("qty") or 0)
        phrase = self._place_order(ticker, "Buy", quote_usdt=quote, maker=True)
        _cash, held = self._cash_and_held()
        after = float((held.get(ticker) or {}).get("qty") or 0)
        bought = after - before
        if bought > 0:
            write_btc_dip(bought, quote / bought, low=float(arm["low"]))
            arms_now = read_entry_arms()
            write_entry_arms({}, None)
            logger.info("[Крипта] карман %s: купил на %.0f$ на отскоке от дна", ticker, quote)
        return phrase

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

    def _probe_bars(self, ticker: str, interval: str, limit: int) -> list[dict[str, float]]:
        cache_sec = 2.0 if str(interval) == "5" else None
        data = self._public_get(
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": self._symbol(ticker),
                "interval": interval,
                "limit": str(limit),
            },
            f"kl:{ticker}:{interval}:probe",
            cache_sec=cache_sec,
        )
        rows = (data.get("result") or {}).get("list") or []
        bars: list[dict[str, float]] = []
        for row in reversed(rows):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                raw_ts = float(row[0])
                ts_sec = raw_ts / 1000.0 if raw_ts > 1e11 else raw_ts
                bars.append({
                    "ts": ts_sec,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (TypeError, ValueError):
                continue
        return bars

    def _probe_blocked(self) -> set[str]:
        """Карманы, уже занятые слоты и любые живые позиции щуп не трогает."""
        blocked = {BTC_DIP_TICKER} | second_pocket_tickers()
        # Убыточный выход (MEGA/HAEDAL) — не сразу обратно в ту же монету.
        blocked |= set(crypto_journal.cooldown_tickers(cfg().cooldown_hours))
        # Недавняя продажа — блок; если цена уже выше продажи — продолжение хода, пускаем.
        recent = set(crypto_journal.recently_sold(PROBE_REENTRY_HOURS))
        if recent:
            try:
                tape = {str(r.get("ticker") or "").upper(): r for r in self._tape()}
            except Exception:
                tape = {}
            for ticker in list(recent):
                sell_px = crypto_journal.last_sell_price(ticker)
                live = float((tape.get(ticker) or {}).get("price") or 0)
                if (
                    sell_px
                    and live > 0
                    and live >= sell_px * (1.0 + PROBE_REENTRY_BREAKOUT_PCT / 100.0)
                ):
                    recent.discard(ticker)
            blocked |= recent
        try:
            _cash, positions = self._wallet()
        except Exception as exc:
            logger.info("[Крипта] щуп кошелёк: %s", exc)
            return blocked | read_blacklist()
        for item in positions:
            if float(item.get("value") or 0) >= ALT_HELD_MIN_USD:
                blocked.add(str(item["ticker"]))
        return blocked | read_blacklist()

    def _probe_week(self, ticker: str) -> float | None:
        try:
            mom = self._momentum(ticker)
        except Exception as exc:
            logger.info("[Крипта] щуп %s неделя: %s", ticker, exc)
            return None
        chg = mom.get("chg_7") if isinstance(mom, dict) else None
        if chg is None:
            return None
        try:
            return float(chg)
        except (TypeError, ValueError):
            return None

    def _probe_cycle(self) -> None:
        with common._TRADE_LOCK:
            self._probe_locked()

    def _probe_locked(self) -> None:
        state = read_probe()
        for leg in probe_legs(state):
            self._probe_exit(leg)
        if not cfg().probe_on:
            return
        state = read_probe()
        for arm in probe_arms(state):
            self._probe_arm({
                "arm_ticker": arm["ticker"],
                "arm_shelf": arm["shelf"],
                "arm_ts": arm["ts"],
                "arm_px": arm["px"],
                "arm_algo": arm["algo"],
            })
        state = read_probe()
        if len(probe_legs(state)) + len(probe_arms(state)) < PROBE_MAX_LEGS:
            self._probe_scan()

    def _probe_exit(self, state: dict[str, Any]) -> None:
        ticker = str(state.get("ticker") or "")
        try:
            px = self._ticker(ticker)
            price = float(px.get("price") or 0)
        except Exception as exc:
            logger.info("[Крипта] щуп %s цена: %s", ticker, exc)
            return
        trend = "bull"
        try:
            bars = self._probe_bars(ticker, "15", 80)
            if bars:
                trend = ma_trend([bar["close"] for bar in bars])
        except Exception as exc:
            logger.info("[Крипта] щуп %s тренд: %s", ticker, exc)
        peak = max(float(state.get("peak") or 0), price)
        leg_algo = str(state.get("algo") or "")
        if peak > float(state.get("peak") or 0) + 1e-12:
            write_probe(
                ticker=ticker,
                qty=float(state.get("qty") or 0),
                entry=float(state.get("entry") or 0),
                shelf=float(state.get("shelf") or 0),
                arm_px=float(state.get("arm_px") or 0),
                peak=peak,
                low=float(state.get("low") or 0),
                algo=leg_algo,
            )
        reason = probe_exit_reason(
            price=price,
            entry=float(state.get("entry") or 0),
            shelf=float(state.get("shelf") or 0),
            trend15=trend,
            arm_px=float(state.get("arm_px") or 0),
            peak=peak,
            entry_low=float(state.get("low") or 0),
            algo=leg_algo,
            qty=float(state.get("qty") or 0),
        )
        if not reason:
            return
        qty = float(state.get("qty") or 0)
        entry = float(state.get("entry") or 0)
        # Мягкий выход → карман2 только если уже был реальный плюс (ловушка).
        # Иначе BLEND/GROVE: handoff без профита → течь в hold.
        seed_high = max(float(state.get("peak") or 0), float(price or 0))
        peak_gain = (seed_high / entry - 1.0) * 100.0 if entry > 0 else 0.0
        arm_need = exit_trap_arm_pct(entry=entry, qty=qty) if entry > 0 and qty > 0 else 9e9
        if (
            probe_should_handoff(reason)
            and cfg().second_on
            and qty > 0
            and entry > 0
            and peak_gain >= arm_need - 1e-6
        ):
            # low не тащим: иначе карман2 сразу бьёт «дно» по старому low щупа.
            ok = upsert_second_pocket_leg(
                ticker=ticker,
                qty=qty,
                entry=entry,
                low=0.0,
                exit_high=seed_high,
                exit_ts=time.time() if seed_high > 0 else 0.0,
                max_legs=pocket_from_probe_max(),
            )
            if ok:
                write_probe(drop=ticker)
                logger.info(
                    "[Крипта] щуп %s: передача во второй карман (%s) %.6g @ %.4g",
                    ticker,
                    reason,
                    qty,
                    entry,
                )
                return
            logger.info(
                "[Крипта] щуп %s: карман2 полон — продаю (%s)",
                ticker,
                reason,
            )
        elif probe_should_handoff(reason) and entry > 0 and peak_gain < arm_need - 1e-6:
            logger.info(
                "[Крипта] щуп %s: без плюса не в карман2 (%s, пик %+.2f%% < %.2f%%) — продаю",
                ticker,
                reason,
                peak_gain,
                arm_need,
            )
        try:
            _cash, held = self._cash_and_held()
            wallet_qty = float((held.get(ticker) or {}).get("qty") or 0)
        except Exception:
            wallet_qty = qty
        sell_qty = min(qty, wallet_qty)
        if sell_qty <= 0:
            write_probe(drop=ticker)
            return
        seen = getattr(self, "_last_fill", None)
        try:
            self._place_order(ticker, "Sell", base_qty=sell_qty, price=price, maker=True)
        except Exception as exc:
            logger.info("[Крипта] щуп %s продажа не прошла: %s", ticker, exc)
            return
        fill = getattr(self, "_last_fill", None)
        sold = float(fill.get("qty") or 0) if fill is not seen and isinstance(fill, dict) else sell_qty
        left = max(0.0, qty - sold)
        if left * price >= _MIN_QUOTE:
            write_probe(
                ticker=ticker,
                qty=left,
                entry=entry,
                shelf=float(state.get("shelf") or 0),
                arm_px=float(state.get("arm_px") or 0),
                peak=float(state.get("peak") or 0),
                low=float(state.get("low") or 0),
            )
            logger.info("[Крипта] щуп %s ещё %.6g монет после неполной продажи", ticker, left)
            return
        write_probe(drop=ticker)
        logger.info(
            "[Крипта] щуп %s: выход (%s) %.6g по %.4g",
            ticker,
            reason,
            sold,
            price,
        )

    def _probe_arm(self, state: dict[str, Any]) -> None:
        ticker = str(state.get("arm_ticker") or "")
        shelf = float(state.get("arm_shelf") or 0)
        try:
            px = self._ticker(ticker)
            price = float(px.get("price") or 0)
            day = float(px.get("chg") or 0)
        except Exception as exc:
            logger.info("[Крипта] щуп %s цена: %s", ticker, exc)
            return
        try:
            bars = self._probe_bars(ticker, "15", 80)
            trend = ma_trend([bar["close"] for bar in bars])
        except Exception as exc:
            logger.info("[Крипта] щуп %s тренд: %s", ticker, exc)
            return
        week = self._probe_week(ticker)
        algo = str(state.get("arm_algo") or "")
        params = probe_algo_params(algo)
        action = probe_arm_action(
            armed_ts=float(state.get("arm_ts") or 0),
            now=time.time(),
            price=price,
            shelf=shelf,
            trend15=trend,
            day_chg=day,
            arm_px=float(state.get("arm_px") or 0),
            week_chg=week,
            algo=algo,
            hold_sec=float(params["hold_sec"]),
            chase_pct=float(params["chase_pct"]),
        )
        if action == "wait":
            return
        if action == "drop":
            write_probe(drop_arm=ticker)
            logger.info(
                "[Крипта] щуп %s снят (%s): полка %.4g сейчас %.4g тренд %s",
                ticker,
                algo or "shelf",
                shelf,
                price,
                trend,
            )
            return
        # За hold цена часто доезжает до хая — перед buy ещё раз режем tip.
        if str(algo or "") == PROBE_ALGO_GRIND and bars:
            live = bars[-1]
            if not probe_grind_live_ok(
                price,
                float(live.get("high") or 0),
                float(live.get("low") or 0),
            ):
                write_probe(drop_arm=ticker)
                logger.info(
                    "[Крипта] щуп %s снят (grind tip): сейчас %.4g high %.4g low %.4g",
                    ticker,
                    price,
                    float(live.get("high") or 0),
                    float(live.get("low") or 0),
                )
                return
        if str(algo or "") == PROBE_ALGO_CANDLE:
            # Актуальных 5м: buy_ok мягче live_ok — иначе hold убивает вход (CPOOL).
            try:
                bars5 = self._probe_bars(ticker, "5", 20)
            except Exception as exc:
                logger.info("[Крипта] щуп %s 5м tip: %s", ticker, exc)
                write_probe(drop_arm=ticker)
                return
            live = bars5[-1] if bars5 else {}
            if not probe_candle_buy_ok(
                price,
                float(live.get("high") or 0),
                float(live.get("low") or 0),
                float(live.get("open") or 0),
            ):
                write_probe(drop_arm=ticker)
                logger.info(
                    "[Крипта] щуп %s снят (candle tip): сейчас %.4g open %.4g high %.4g",
                    ticker,
                    price,
                    float(live.get("open") or 0),
                    float(live.get("high") or 0),
                )
                return
        self._probe_buy(
            ticker,
            shelf,
            price,
            arm_px=float(state.get("arm_px") or price),
            entry_low=float(bars[-1].get("low") or 0) if bars else 0.0,
            algo=algo or "shelf",
        )

    def _probe_leg_prices(self, legs: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for leg in legs:
            name = str(leg.get("ticker") or "").upper()
            if not name:
                continue
            try:
                out[name] = float(self._ticker(name)["price"])
            except Exception:
                continue
        return out

    def _probe_buy(
        self,
        ticker: str,
        shelf: float,
        price: float,
        arm_px: float = 0.0,
        entry_low: float = 0.0,
        algo: str = "",
    ) -> None:
        free_cash, held_now = self._cash_and_held()
        legs = probe_legs()
        want = probe_quote_for_buy(legs=legs, prices=self._probe_leg_prices(legs))
        quote = min(want, free_cash * common._BUY_CASH_BUFFER)
        if quote < want * 0.9:
            logger.info("[Крипта] щуп %s: кэша мало на %.0f$", ticker, want)
            return
        before = float((held_now.get(ticker) or {}).get("qty") or 0)
        # Свеча/подножка: market — иначе разгон уходит, пока maker висит.
        maker = str(algo or "") in {"", "shelf"}
        self._place_order(ticker, "Buy", quote_usdt=quote, price=price, maker=maker)
        _cash, held = self._cash_and_held()
        after = float((held.get(ticker) or {}).get("qty") or 0)
        bought = after - before
        if bought <= 0:
            return
        write_probe(
            ticker=ticker,
            qty=bought,
            entry=quote / bought,
            shelf=shelf,
            arm_px=arm_px or price,
            peak=price,
            low=entry_low,
            algo=algo or "shelf",
        )
        tag = ""
        if len(legs) >= 2 and want >= PROBE_BOOST_USD - 1e-6:
            tag = " (3я, boost $60)"
        elif len(probe_legs()) > 1:
            tag = " (второй)"
        logger.info(
            "[Крипта] щуп %s: купил на %.0f$ над полкой %.4g (%s)%s",
            ticker,
            quote,
            shelf,
            algo or "shelf",
            tag,
        )

    def _probe_try_arm(
        self,
        row: dict[str, Any],
        setup: dict[str, Any],
        bars15: list[dict[str, Any]],
        hour: list[dict[str, Any]],
    ) -> bool:
        """Взвод, если тренд/неделя ок. True — слот занят, скан стоп."""
        ticker = str(row.get("ticker") or "")
        week = self._probe_week(ticker)
        algo = str(setup.get("algo") or "shelf")
        if week is None or not probe_trends_ok(
            ma_trend([bar["close"] for bar in bars15]),
            ma_trend([bar["close"] for bar in hour]),
            float(row.get("chg") or 0),
            week_chg=week,
            algo=algo,
        ):
            return False
        hold = int(float(setup.get("hold_sec") or PROBE_ENTRY_HOLD_SEC))
        write_probe(
            arm_ticker=ticker,
            arm_shelf=float(setup["shelf"]),
            arm_ts=time.time(),
            arm_px=float(setup["price"]),
            arm_algo=algo,
        )
        logger.info(
            "[Крипта] щуп %s %s полка %.4g сейчас %.4g — жду %dс",
            ticker,
            algo,
            setup["shelf"],
            setup["price"],
            hold,
        )
        return True

    def _probe_scan(self) -> None:
        if len(probe_legs()) + len(probe_arms()) >= PROBE_MAX_LEGS:
            return
        try:
            tape = self._tape()
        except Exception as exc:
            logger.info("[Крипта] щуп лента: %s", exc)
            return
        blocked = self._probe_blocked() | probe_tickers() | {arm["ticker"] for arm in probe_arms()}
        candidates: list[dict[str, Any]] = []
        for row in tape:
            ticker = str(row.get("ticker") or "")
            if not ticker or ticker in blocked or ticker in SLEEVE_SKIP or _leveraged_base(ticker):
                continue
            candidates.append(row)
        ranked = probe_scan_ranked_rows(candidates)
        order = probe_scan_algo_order()
        classic = tuple(a for a in order if a != PROBE_ALGO_CANDLE)

        def _arm_slots_left() -> int:
            return PROBE_MAX_LEGS - len(probe_legs()) - len(probe_arms())

        def _try_arm_row(row: dict[str, Any], setup: dict[str, Any], bars15: list, hour: list) -> bool:
            ticker = str(row.get("ticker") or "")
            if self._probe_try_arm(row, setup, bars15, hour):
                blocked.add(ticker)
                return True
            return False

        # 1) Широкий обзор: только 5м candle — 15м/1h только если свеча уже живая.
        for _rank, row in ranked[:PROBE_SCAN_LIMIT]:
            if _arm_slots_left() <= 0:
                break
            ticker = str(row["ticker"])
            if ticker in blocked:
                continue
            try:
                bars5 = self._probe_bars(ticker, "5", 80)
            except Exception as exc:
                logger.info("[Крипта] щуп %s 5м: %s", ticker, exc)
                continue
            setup = probe_pick_setup(
                [],
                bars5,
                order=(PROBE_ALGO_CANDLE,),
                day_chg=float(row.get("chg") or 0),
                day_high=float(row.get("high") or 0),
                now=time.time(),
            )
            if not setup:
                continue
            try:
                bars15 = self._probe_bars(ticker, "15", 120)
                hour = self._probe_bars(ticker, "60", 80)
            except Exception as exc:
                logger.info("[Крипта] щуп %s тренд: %s", ticker, exc)
                continue
            _try_arm_row(row, setup, bars15, hour)
        # 2) Узкий топ: impulse/sprint/shelf. Пусто, пока в ORDER только candle.
        if not classic or _arm_slots_left() <= 0:
            return
        for _rank, row in ranked[:PROBE_SCAN_CLASSIC_LIMIT]:
            if _arm_slots_left() <= 0:
                break
            ticker = str(row["ticker"])
            if ticker in blocked:
                continue
            try:
                bars15 = self._probe_bars(ticker, "15", 120)
            except Exception as exc:
                logger.info("[Крипта] щуп %s свечи: %s", ticker, exc)
                continue
            try:
                bars5 = self._probe_bars(ticker, "5", 80)
            except Exception as exc:
                logger.info("[Крипта] щуп %s 5м: %s", ticker, exc)
                bars5 = []
            setup = probe_pick_setup(
                bars15,
                bars5,
                order=classic,
                day_chg=float(row.get("chg") or 0),
                day_high=float(row.get("high") or 0),
            )
            if not setup:
                continue
            try:
                hour = self._probe_bars(ticker, "60", 80)
            except Exception as exc:
                logger.info("[Крипта] щуп %s час: %s", ticker, exc)
                continue
            _try_arm_row(row, setup, bars15, hour)

    def _spot_buy_vwap(self, ticker: str, need_qty: float) -> float | None:
        """Средняя цена ручной покупки по свежим fill Bybit (spot)."""
        need = float(need_qty or 0)
        if need <= 0:
            return None
        try:
            data = self._signed(
                "GET",
                "/v5/execution/list",
                {
                    "category": "spot",
                    "symbol": self._symbol(ticker),
                    "limit": "50",
                },
            )
        except Exception as exc:
            logger.info("[Крипта] ловушка %s история сделок: %s", ticker, exc)
            return None
        rows = (data.get("result") or {}).get("list") or []
        got = 0.0
        cost = 0.0
        for row in rows:
            if str(row.get("side") or "").lower() != "buy":
                continue
            try:
                px = float(row.get("execPrice") or 0)
                qty = float(row.get("execQty") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or qty <= 0:
                continue
            take = min(qty, need - got)
            cost += take * px
            got += take
            if got + 1e-12 >= need:
                break
        if got <= 0 or cost <= 0:
            return None
        return cost / got

    def _manual_entry_px(self, ticker: str, qty: float, mark: float) -> float:
        """Вход для ловушки: VWAP по бирже, иначе mark (хуже — ловушка от текущей)."""
        vwap = self._spot_buy_vwap(ticker, qty)
        if vwap and vwap > 0:
            return float(vwap)
        # Журнал стола — если покупка шла через Jarvis.
        try:
            trades = crypto_journal._read_trades()
        except Exception:
            trades = []
        got = 0.0
        cost = 0.0
        for row in reversed(list(trades or [])):
            if str(row.get("ticker") or "").upper() != ticker:
                continue
            if str(row.get("side") or "").lower() != "buy":
                continue
            try:
                px = float(row.get("price") or 0)
                q = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or q <= 0:
                continue
            take = min(q, float(qty) - got)
            cost += take * px
            got += take
            if got + 1e-12 >= float(qty):
                break
        if got > 0 and cost > 0:
            return cost / got
        return float(mark or 0)

    def _claim_manual_trap_legs(
        self,
        positions: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Любые свободные ноги на балансе (кроме BTC и активных щупа/SOL-dip) — в карман2 под ловушку вершины."""
        notes: list[str] = []
        probe_qty: dict[str, float] = {}
        for leg in probe_legs():
            coin = str(leg.get("ticker") or "").upper()
            if coin:
                probe_qty[coin] = probe_qty.get(coin, 0.0) + float(leg.get("qty") or 0)
        dip_qty = float(read_btc_dip().get("qty") or 0)
        pocketed = {str(leg["ticker"]) for leg in read_second_pocket_legs()}
        for ticker, pos in positions.items():
            coin = str(ticker or "").upper()
            if (
                not coin
                or coin in MANUAL_TRAP_SKIP
                or coin in SLEEVE_SKIP
                or _leveraged_base(coin)
            ):
                continue
            held = float(pos.get("qty") or 0)
            price = float(pos.get("price") or 0)
            value = float(pos.get("value") or 0) or held * price
            if held <= 0 or value < ALT_HELD_MIN_USD:
                continue
            target = manual_trap_free_qty(
                held,
                probe_qty=float(probe_qty.get(coin) or 0),
                pocket_qty=0.0,
                dip_qty=dip_qty if coin == BTC_DIP_TICKER else 0.0,
            )
            if target * price < max(_MIN_QUOTE, ALT_HELD_MIN_USD):
                continue
            # Уже в кармане2 — подтянуть qty к свободному остатку (минус щуп/SOL-dip).
            if coin in pocketed:
                reserved = second_pocket_qty(coin)
                if abs(target - reserved) > max(1e-8, reserved * 1e-6):
                    book = next(
                        (leg for leg in read_second_pocket_legs() if str(leg["ticker"]) == coin),
                        None,
                    )
                    if book:
                        update_second_pocket_leg(
                            coin,
                            qty=target,
                            entry=float(book.get("entry") or 0),
                            low=float(book.get("low") or 0),
                            exit_high=float(book.get("exit_high") or 0),
                            exit_ts=float(book.get("exit_ts") or 0),
                        )
                        notes.append(f"{coin} qty→{target:.6g}")
                continue
            entry = self._manual_entry_px(coin, target, price)
            if entry <= 0:
                continue
            ok = upsert_second_pocket_leg(
                ticker=coin,
                qty=target,
                entry=entry,
                max_legs=MANUAL_TRAP_MAX_LEGS,
            )
            if ok:
                notes.append(f"{coin} вход {entry:.6g} qty {target:.6g}")
                logger.info(
                    "[Крипта] ловушка вручную %s: qty %.6g вход %.6g (нужен +%.2f%%)",
                    coin,
                    target,
                    entry,
                    exit_trap_arm_pct(entry=entry, qty=target),
                )
        return notes

    def _pockets_tick(self) -> None:
        """Раз в минуту SOL: локальный откат; второй карман — handoff и ручные ноги."""
        if not getattr(self, "_api_key", ""):
            return
        with common._TRADE_LOCK:
            self._reconcile_desk_state()
            try:
                cash, positions_list = self._wallet()
            except Exception as exc:
                logger.info("[Крипта] карманы кошелёк: %s", exc)
                return
            positions = {item["ticker"]: item for item in positions_list}
            equity = max(cash + sum(float(item.get("value") or 0) for item in positions_list), 1.0)
            prices = {
                str(item.get("ticker") or "").upper(): float(item.get("price") or 0)
                for item in positions_list
                if item.get("ticker")
            }
            claim_notes = self._claim_manual_trap_legs(positions)
            if claim_notes:
                logger.info("[Крипта] ловушка вручную: %s", "; ".join(claim_notes))
            for pocket_leg in read_second_pocket_legs():
                coin = str(pocket_leg["ticker"])
                if coin not in prices:
                    try:
                        prices[coin] = float(self._ticker(coin).get("price") or 0)
                    except Exception:
                        prices[coin] = 0.0
            self._trade_second_pocket_legs(
                positions=positions,
                prices=prices,
                blocked=set(),
            )
            ticker = BTC_DIP_TICKER
            try:
                quote = self._ticker(ticker)
            except Exception as exc:
                logger.info("[Крипта] карман %s цена: %s", ticker, exc)
                return
            price = float(quote.get("price") or 0)
            day = quote.get("chg")
            sol_on = cfg().pocket_on
            arms = read_entry_arms()
            if sol_on:
                tape = getattr(self, "_bybit_tape", None)
                taped = tape.entry_lows() if tape is not None else {}
                updated = self._swing_pocket_arm(
                    ticker,
                    arms.get("pocket"),
                    price=price,
                    day_chg=None if day is None else float(day),
                    now=time.time(),
                    taped=taped,
                )
                write_entry_arms({}, updated, sui=False)
            else:
                write_entry_arms({}, None, sui=False)
            self._sync_tape_symbols()
            self._trade_btc_dip(
                day_chg=None if day is None else float(day),
                positions=positions,
                price=price,
                blocked=False,
                equity=equity,
                allow_entry=sol_on,
                ticker=ticker,
            )

    def _maybe_daily_report(self) -> None:
        if not common.telegram_configured():
            return
        if not crypto_journal.should_send_daily_report():
            return
        try:
            realized = crypto_journal.read_lifetime_pnl()
            earned = None if realized is None else realized + self._earn_interest()
            text = crypto_journal.format_daily_pnl_report(lifetime=earned)
            common.send_telegram_notification(text, background=False)
            crypto_journal.mark_daily_report_sent()
            logger.info("[Крипта] дневной отчёт отправлен.")
        except Exception as exc:
            logger.warning("[Крипта] дневной отчёт: %s", exc)

    def _desk_loop(self) -> None:
        try:
            self._ensure_tape()
            self._sync_tape_symbols()
        except Exception as exc:
            logger.warning("[Крипта] лента старт: %s", exc)
        self._desk_stop.wait(90)
        next_probe = 0.0
        next_report = time.time() + _DESK_PERIOD_SEC
        while not self._desk_stop.is_set():
            if not self._desk_ready():
                if self._desk_stop.wait(2.0):
                    return
                continue
            now = time.time()
            if next_probe <= 0:
                next_probe = now
            armed = bool(probe_arms())
            gap = PROBE_ARMED_PERIOD_SEC if armed else probe_period_sec()
            wake_at = min(next_probe, next_report)
            tape = getattr(self, "_bybit_tape", None)
            while time.time() < wake_at:
                remaining = min(2.0, wake_at - time.time())
                if remaining <= 0:
                    break
                if tape is not None:
                    fired = tape.wait(remaining)
                    if self._desk_stop.is_set():
                        return
                    if fired:
                        self._pockets_tick()
                        break
                elif self._desk_stop.wait(remaining):
                    return
                if not self._desk_ready():
                    break
            if not self._desk_ready():
                continue
            now = time.time()
            try:
                self._reload_env()
                self._refresh_sleeve()
            except Exception as exc:
                logger.warning("[Крипта] env: %s", exc)
            if now >= next_report:
                try:
                    self._maybe_daily_report()
                except Exception as exc:
                    logger.warning("[Крипта] отчёт: %s", exc)
                next_report = time.time() + _DESK_PERIOD_SEC
            if now >= next_probe:
                try:
                    self._probe_cycle()
                    self._sync_tape_symbols()
                except Exception as exc:
                    logger.warning("[Крипта] щуп: %s", exc)
                try:
                    self._pockets_tick()
                except Exception as exc:
                    logger.warning("[Крипта] карманы: %s", exc)
                armed = bool(probe_arms())
                gap = PROBE_ARMED_PERIOD_SEC if armed else probe_period_sec()
                next_probe = time.time() + gap
        tape = getattr(self, "_bybit_tape", None)
        if tape is not None:
            tape.stop()
