# skills/crypto/desk.py
# Автостол: режим BTC, скор без Groq, коридор ребаланса.
# Советник (crypto_advisor) по-прежнему зовёт ai_pick_alloc / Groq.

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
    _STABLES,
    _WATCH_PERIOD_SEC,
    _min_trade_usd,
    _read_ticker_set,
    _spoken,
    _write_ticker_set,
    parse_ai_alloc,
    read_alloc_state,
    read_btc_dip,
    read_trail_state,
    write_alloc_state,
    write_btc_dip,
    write_trail_state,
)
from .desk_policy import (
    ALT_BB_INTERVAL,
    ALT_HELD_MIN_USD,
    ALT_MAX_NAMES,
    ALT_MIN_HOLD_HOURS,
    CHURN_COOLDOWN_HOURS,
    DESK_ALT_SLEEVE,
    BTC_DIP_TICKER,
    btc_dip_quote,
    pocket_cash_reserve,
    alt_exit_reason,
    btc_dip_exit_reason,
    btc_dip_should_buy,
    alt_slot_pct,
    band_pct_for,
    cash_floor_pct,
    filter_auto_candidates,
    is_risk_off,
    pick_watch_alt_entries,
    score_alloc,
    should_rebalance_leg,
    should_watch_dip_buy,
    take_profit_day_pct_for,
    trail_pct_for,
    update_trail_leg,
    update_exclusions,
    watch_rate_ok,
)
from . import journal as crypto_journal

logger = logging.getLogger(__name__)


def _wants_advice(text: str) -> bool:
    return any(hint in text for hint in _ADVICE_HINTS)


def next_desk_at(state: dict[str, Any] | None, now: float) -> float:
    """После старта отсчёт от последнего полного стола: перезапуск не откладывает его ещё на период."""
    if state is None:
        return now
    return max(now, float(state.get("desk_ts") or 0) + _DESK_PERIOD_SEC)


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

    def desk_candidates(self) -> list[dict[str, Any]]:
        tape = self._tape()
        from_tape = {row["ticker"]: row for row in tape}
        held: dict[str, float] = {}
        held_values: dict[str, float] = {}
        if self._api_key:
            try:
                _cash, positions = self._wallet()
                held = {item["ticker"]: item["qty"] for item in positions}
                held_values = {item["ticker"]: float(item["value"] or 0) for item in positions}
            except Exception as exc:
                logger.warning("[Крипта] кошелёк для стола: %s", exc)
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            ticker = row["ticker"]
            if ticker in seen or ticker in _STABLES:
                return
            seen.add(ticker)
            mom = {"chg_7": None, "trend": "нет данных"}
            try:
                mom = self._momentum(ticker)
            except Exception:
                pass
            bb: dict[str, Any] = {}
            if ticker in DESK_ALT_SLEEVE:
                bands = self._alt_bands(ticker)
                if bands:
                    bb = {"bb_z": bands["z"], "bb_ma": bands["ma"]}
            held_value = held_values.get(ticker, 0.0)
            if ticker == BTC_DIP_TICKER:
                dip = read_btc_dip()
                px = float(row.get("price") or 0)
                held_value = max(0.0, held_value - float(dip.get("qty") or 0) * px)
            chosen.append({
                **row,
                "held": held.get(ticker, 0),
                "held_value": held_value,
                **mom,
                **bb,
            })

        for ticker in list(held) + list(self._watchlist) + list(DESK_ALT_SLEEVE):
            row = from_tape.get(ticker)
            if row is None:
                try:
                    px = self._ticker(ticker)
                    row = {
                        "ticker": ticker,
                        "name": _spoken(ticker),
                        "price": px["price"],
                        "chg": px["chg"],
                        "turnover": px["turnover"],
                    }
                except Exception:
                    continue
            add(row)
        for row in tape:
            if len(chosen) >= 24:
                break
            add(row)
        return chosen

    def _alt_bands(self, ticker: str) -> dict[str, float] | None:
        try:
            return self._bands(ticker, ALT_BB_INTERVAL)
        except Exception as exc:
            logger.info("[Крипта] полосы %s: %s", ticker, exc)
            return None

    def _btc_regime(self) -> tuple[float | None, float | None]:
        chg7 = None
        chg_day = None
        try:
            mom = self._momentum("BTC")
            chg7 = mom.get("chg_7")
            if chg7 is not None:
                chg7 = float(chg7)
        except Exception:
            pass
        try:
            px = self._ticker("BTC")
            chg_day = float(px.get("chg") or 0)
        except Exception:
            pass
        return chg7, chg_day

    def desk_auto_candidates(self) -> list[dict[str, Any]]:
        """Кандидаты только для автостола (whitelist + фильтры + анти-чёрн)."""
        rows = self.desk_candidates()
        held: set[str] = set()
        for row in rows:
            if float(row.get("held") or 0) > 0:
                held.add(str(row.get("ticker") or "").upper())
        cool = crypto_journal.cooldown_tickers(CHURN_COOLDOWN_HOURS)
        recent = crypto_journal.recently_bought(CHURN_COOLDOWN_HOURS)
        # Недавняя покупка не блок на ребаланс уже держанного, но не даём набирать новую с нуля.
        cool |= {t for t in recent if t not in held}
        return filter_auto_candidates(
            rows,
            held=held,
            watchlist=list(self._watchlist) + list(DESK_ALT_SLEEVE),
            cooldown=cool,
        )

    def _held_alt_alloc(self) -> dict[str, float]:
        """Держанные альты рукава: их слоты сохраняются, пока не выйдут у средней или по стопу."""
        try:
            _cash, positions = self._wallet()
        except Exception:
            return {}
        held = sorted(
            (
                p for p in positions
                if p["ticker"] in DESK_ALT_SLEEVE
                and not self._is_owner_position(p["ticker"])
                and float(p.get("value") or 0) >= ALT_HELD_MIN_USD
            ),
            key=lambda p: float(p.get("value") or 0),
            reverse=True,
        )[:ALT_MAX_NAMES]
        return {p["ticker"]: alt_slot_pct() for p in held}

    def desk_score_alloc(self, candidates: list[dict[str, Any]] | None = None) -> tuple[dict[str, float], str]:
        btc7, btc_day = self._btc_regime()
        rows = candidates if candidates is not None else self.desk_auto_candidates()
        excluded = self._sync_exclusions(rows)
        if is_risk_off(btc_chg_7=btc7, btc_chg_day=btc_day):
            why = (
                f"Риск-офф по BTC (7д {btc7:+.1f}%, сутки {btc_day:+.1f}%) — новые альты не беру."
                if btc7 is not None and btc_day is not None
                else "Риск-офф по BTC — новые альты не беру."
            )
            return self._held_alt_alloc(), why
        if not rows:
            return {}, "Нет кандидатов для автостола."
        floor = cash_floor_pct(btc_chg_7=btc7, btc_chg_day=btc_day)
        return score_alloc(rows, cash_floor_pct=floor, excluded=excluded)

    def ai_pick_alloc(self, candidates: list[dict[str, Any]]) -> tuple[dict[str, float], str]:
        """Только для советника / голоса «посоветуй». Автостол сюда не ходит."""
        allowed = {str(row.get("ticker") or "").upper() for row in candidates if row.get("ticker")}
        facts = self._alloc_facts(candidates)
        btc7, btc_day = self._btc_regime()
        if is_risk_off(btc_chg_7=btc7, btc_chg_day=btc_day):
            return {}, (
                f"Риск-офф по BTC (7д {btc7:+.1f}%) — лучше кэш."
                if btc7 is not None
                else "Риск-офф по BTC — лучше кэш."
            )
        try:
            from skill_settings import get_effective_groq_model
            from skills.groq_client import complete

            raw = complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты Джарвис. Выбираешь спотовый портфель на Bybit в USDT. "
                            "Без плеча, без фьючерсов, без стейблов в alloc. "
                            "Ответ строго JSON: {\"alloc\": {\"BTC\": 40, \"ETH\": 30}, "
                            "\"why\": \"коротко по-русски до 180 знаков\"}. "
                            "Только тикеры из списка. Сумма до 100, остаток — кэш USDT. "
                            "Если дан Fear & Greed — учти как фон (extreme fear ≠ обязательно всё в кэш; "
                            "extreme greed — осторожнее с новыми покупками). "
                            "Не выдумывай новости и цифры. Если картина плохая — пустой alloc. "
                            "Не обещай прибыль. Мужской род."
                        ),
                    },
                    {"role": "user", "content": facts + "\n\nВыбери доли."},
                ],
                preferred=get_effective_groq_model(),
                temperature=0.2,
                max_tokens=250,
            )
            alloc, why = parse_ai_alloc(raw, allowed)
            if alloc:
                return alloc, why
        except Exception as exc:
            logger.warning("[Крипта] ИИ-выбор: %s", exc)
        floor = cash_floor_pct(btc_chg_7=btc7, btc_chg_day=btc_day)
        return score_alloc(candidates, cash_floor_pct=floor)

    def _alloc_facts(self, candidates: list[dict[str, Any]]) -> str:
        cash = 0.0
        book = "счёт недоступен"
        if self._api_key:
            try:
                cash, positions = self._wallet()
                parts = [f"кэш {cash:.0f} USDT"]
                for item in positions[:6]:
                    parts.append(f"{item['name']} ({item['ticker']}) {item['value']:.0f}")
                book = "; ".join(parts)
            except Exception:
                book = "счёт недоступен"
        cool = crypto_journal.cooldown_tickers(CHURN_COOLDOWN_HOURS)
        lines = [f"Портфель: {book}"]
        try:
            from .sentiment import fear_greed_line

            fng = fear_greed_line()
            if fng:
                lines.append(fng)
        except Exception as exc:
            logger.info("[Крипта] Fear & Greed в фактах: %s", exc)
        lines.append("Кандидаты спот USDT:")
        if cool:
            lines.append("Недавно убыточные продажи (не рекомендуй): " + ", ".join(sorted(cool)))
        for row in candidates[:12]:
            mom = row.get("chg_7")
            mom_s = f", 7д {mom:+.1f}%" if mom is not None else ""
            lines.append(
                f"{row.get('name')} ({row.get('ticker')}) "
                f"{row.get('price')} USDT, сутки {row.get('chg'):+.1f}%{mom_s}, "
                f"оборот {row.get('turnover'):.0f}"
            )
        return "\n".join(lines)

    def _trade_auto(self, silent: bool = False) -> str:
        with common._TRADE_LOCK:
            try:
                return self._trade_auto_locked(silent)
            finally:
                self._earn_park_idle()

    def _trade_auto_locked(self, silent: bool = False) -> str:
        # Авто и «поторгуй» — скор + режим BTC, без Groq.
        candidates = self.desk_auto_candidates()
        alloc, why = self.desk_score_alloc(candidates)
        state = read_alloc_state() or {}
        watch_trades = list(state.get("watch_trades") or [])
        if not alloc:
            write_alloc_state({}, why=why or "кэш", watch_trades=watch_trades, desk_ts=time.time())
            result = why or "Стол оставил кэш."
            if silent:
                logger.info("[Крипта] авто: %s", result)
            return result
        desc = ", ".join(f"{_spoken(ticker)} {int(round(pct))}%" for ticker, pct in alloc.items())
        logger.info("[Крипта] целевой портфель: %s (%s)", desc, why)
        write_alloc_state(alloc, why=why, watch_trades=watch_trades, desk_ts=time.time())
        if not self._api_key:
            return f"Целевой портфель: {desc}. Ключа Bybit нет. {why}".strip()
        result = self._rebalance(alloc)
        if why and not silent:
            return f"{result} {why}".strip()
        return result

    def _desk_watch(self, silent: bool = True) -> str:
        with common._TRADE_LOCK:
            try:
                return self._desk_watch_locked(silent)
            finally:
                self._earn_park_idle()

    def _desk_watch_locked(self, silent: bool = True) -> str:
        """Быстрый дозор: к сохранённой цели. Без нового скора."""
        state = read_alloc_state()
        if state is None:
            if silent:
                logger.info("[Крипта] дозор: нет цели — жду полный стол")
            return "нет цели"
        watch_trades = list(state.get("watch_trades") or [])
        now = time.time()
        if not watch_rate_ok(watch_trades, now=now):
            if silent:
                logger.info("[Крипта] дозор: лимит сделок за час")
            return "лимит дозора"
        alloc = dict(state.get("alloc") or {})
        btc7, btc_day = self._btc_regime()
        if is_risk_off(btc_chg_7=btc7, btc_chg_day=btc_day):
            why = "дозор: риск-офф по BTC — новые альты не беру"
            logger.info("[Крипта] %s", why)
            if not self._api_key:
                write_alloc_state({}, why=why, watch_trades=watch_trades)
                return why
            alloc = self._held_alt_alloc()
            result = self._rebalance(alloc, respect_hold=False)
            traded = result != "Портфель уже в коридоре цели."
            if traded:
                watch_trades.append(now)
            write_alloc_state(alloc, why=why, watch_trades=watch_trades)
            return result if traded else why

        if not self._api_key:
            return "ключа нет"
        result = self._watch_react(alloc)
        if result != "дозор: без сделок":
            watch_trades.append(now)
            write_alloc_state(alloc, why=str(state.get("why") or ""), watch_trades=watch_trades)
            logger.info("[Крипта] дозор: %s", result)
        elif silent:
            logger.info("[Крипта] дозор: без сделок")
        return result

    def _sync_exclusions(self, rows: list[dict[str, Any]]) -> set[str]:
        current = _read_ticker_set(common._EXCLUDED_PATH) or set()
        updated = update_exclusions(current, rows)
        if updated != current:
            _write_ticker_set(common._EXCLUDED_PATH, updated)
            logger.info(
                "[Крипта] исключения: %s",
                ", ".join(sorted(updated)) or "пусто",
            )
        return updated

    def _watch_react(self, target_alloc: dict[str, float]) -> str:
        """Выход альта → новые альты. Меняет target_alloc на месте."""
        for ticker in list(target_alloc):
            if ticker not in DESK_ALT_SLEEVE:
                target_alloc.pop(ticker, None)
        excluded = _read_ticker_set(common._EXCLUDED_PATH) or set()
        cash, positions_list = self._wallet()
        positions = {item["ticker"]: item for item in positions_list}
        self._ensure_desk_bought(set(positions.keys()))
        equity = max(cash + sum(item["value"] for item in positions_list), 1.0)
        min_trade = _min_trade_usd(equity)
        prices = {item["ticker"]: float(item["price"] or 0) for item in positions_list}
        day_chgs: dict[str, float | None] = {}
        for ticker in set(target_alloc) | set(positions) | {BTC_DIP_TICKER}:
            need_px = ticker not in prices or prices[ticker] <= 0
            try:
                px = self._ticker(ticker)
                if need_px:
                    prices[ticker] = float(px.get("price") or 0)
                day_chgs[ticker] = float(px.get("chg") or 0)
            except Exception:
                if need_px:
                    prices[ticker] = 0.0
                day_chgs[ticker] = None
        relevant = set(positions) | set(target_alloc)
        target_values = {
            ticker: equity * (target_alloc.get(ticker, 0.0) / 100.0) for ticker in relevant
        }
        current_values = {
            ticker: float(positions.get(ticker, {}).get("value") or 0) for ticker in relevant
        }
        dip_qty_watch = float(read_btc_dip().get("qty") or 0)
        dip_px_watch = float(prices.get(BTC_DIP_TICKER) or 0)
        if dip_qty_watch > 0 and dip_px_watch > 0 and BTC_DIP_TICKER in current_values:
            current_values[BTC_DIP_TICKER] = max(
                0.0, current_values[BTC_DIP_TICKER] - dip_qty_watch * dip_px_watch
            )
        parts: list[str] = []
        trail_sold: set[str] = set()
        try:
            avg_costs = crypto_journal.open_avg_costs()
        except Exception:
            avg_costs = {}
        trail_legs = read_trail_state()
        new_trail: dict[str, dict[str, Any]] = {}
        for ticker, pos in positions.items():
            if self._is_owner_position(ticker):
                continue
            price = float(prices.get(ticker) or pos.get("price") or 0)
            if price <= 0:
                continue
            # Пыль после продажи не держит трейл — иначе новая покупка унаследует старый пик.
            leg_value = float(pos.get("value") or 0) or float(pos.get("qty") or 0) * price
            if leg_value < min_trade:
                continue
            if trail_pct_for(ticker) is None:
                continue
            prev = trail_legs.get(ticker) or {}
            entry = float(prev.get("entry") or 0) or float(avg_costs.get(ticker) or 0) or price
            updated = update_trail_leg(
                price=price,
                entry=entry,
                high=float(prev.get("high") or 0) or None,
                armed=bool(prev.get("armed")),
                trail_pct=trail_pct_for(ticker),
            )
            new_trail[ticker] = {
                "entry": updated["entry"],
                "high": updated["high"],
                "armed": updated["armed"],
            }
            if not updated["hit"]:
                continue
            qty = float(pos.get("qty") or 0)
            if ticker == BTC_DIP_TICKER:
                qty = max(0.0, qty - float(read_btc_dip().get("qty") or 0))
            value = qty * price
            if qty <= 0 or value < min_trade:
                continue
            try:
                parts.append(self._place_order(ticker, "Sell", base_qty=qty, price=price, maker=True))
                trail_sold.add(ticker)
                time.sleep(0.4)
                logger.info(
                    "[Крипта] трейл %s: high %.4g stop %.4g gain %+.1f%%",
                    ticker,
                    updated["high"],
                    updated["stop"],
                    updated["gain_pct"],
                )
            except Exception as exc:
                logger.warning("[Крипта] трейл продажа %s: %s", ticker, exc)
                continue
            # После срабатывания ногу из трейла убираем.
            new_trail.pop(ticker, None)

        alt_bands: dict[str, dict[str, float] | None] = {}

        def bands_for(ticker: str) -> dict[str, float] | None:
            if ticker not in alt_bands:
                alt_bands[ticker] = self._alt_bands(ticker)
            return alt_bands[ticker]

        for ticker, pos in positions.items():
            if ticker not in DESK_ALT_SLEEVE or ticker in trail_sold:
                continue
            if self._is_owner_position(ticker):
                continue
            price = float(prices.get(ticker) or pos.get("price") or 0)
            qty = float(pos.get("qty") or 0)
            if ticker == BTC_DIP_TICKER:
                qty = max(0.0, qty - float(read_btc_dip().get("qty") or 0))
            value = float(pos.get("value") or 0) or qty * price
            if price <= 0 or qty <= 0 or value < min_trade:
                continue
            entry = float(avg_costs.get(ticker) or 0) or float(
                (new_trail.get(ticker) or {}).get("entry") or 0
            )
            bands = bands_for(ticker)
            reason = alt_exit_reason(price=price, entry=entry, ma=bands["ma"] if bands else None)
            if not reason:
                continue
            try:
                parts.append(self._place_order(ticker, "Sell", base_qty=qty, price=price, maker=True))
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] выход альта %s: %s", ticker, exc)
                continue
            logger.info(
                "[Крипта] альт %s: выход (%s) вход %.4g цена %.4g %+.1f%%",
                ticker,
                reason,
                entry,
                price,
                (price / entry - 1.0) * 100.0,
            )
            trail_sold.add(ticker)
            new_trail.pop(ticker, None)
            target_alloc.pop(ticker, None)
            target_values[ticker] = 0.0
        write_trail_state(new_trail)

        time.sleep(0.3)
        self._bust_private_cache()
        cash, _pos = self._cash_and_held()
        budget = cash * common._BUY_CASH_BUFFER
        reserve = pocket_cash_reserve(
            equity, pocket_open=float(read_btc_dip().get("qty") or 0) > 0
        )
        cash = max(0.0, budget - reserve)

        held_alts = {
            t for t in positions
            if t in DESK_ALT_SLEEVE
            and t not in trail_sold
            and current_values.get(t, 0.0) >= ALT_HELD_MIN_USD
        }
        taken = {t for t in target_alloc if t in DESK_ALT_SLEEVE} | held_alts
        if len(taken) < ALT_MAX_NAMES:
            try:
                blocked = set(crypto_journal.cooldown_tickers(CHURN_COOLDOWN_HOURS))
            except Exception:
                blocked = set()
            blocked |= trail_sold
            blocked |= {t for t in positions if self._is_owner_position(t)}
            for t in DESK_ALT_SLEEVE - taken - blocked:
                bands_for(t)
            slot = alt_slot_pct()
            for t in pick_watch_alt_entries(
                alt_bands,
                target_alloc=target_alloc,
                held_alts=held_alts,
                blocked=blocked,
                excluded=excluded,
            ):
                target_alloc[t] = slot
                target_values[t] = equity * slot / 100.0
                current_values.setdefault(t, 0.0)
                logger.info(
                    "[Крипта] альт %s у нижней полосы 4h (z %.2f) — слот %.1f%%",
                    t,
                    float((alt_bands.get(t) or {}).get("z") or 0),
                    slot,
                )

        buys = []
        for ticker in target_alloc:
            if ticker in trail_sold:
                continue
            need = target_values.get(ticker, 0) - current_values.get(ticker, 0)
            is_alt = ticker in DESK_ALT_SLEEVE
            if not should_watch_dip_buy(
                ticker=ticker,
                day_chg=day_chgs.get(ticker),
                current_value=current_values.get(ticker, 0),
                target_value=target_values.get(ticker, 0),
                min_trade_usd=min_trade,
                bb_z=(bands_for(ticker) or {}).get("z") if is_alt else None,
                excluded=excluded,
            ):
                continue
            # Альт берём слотом сразу.
            quote = max(0.0, need) if is_alt else 0.0
            if quote >= _MIN_QUOTE:
                buys.append((ticker, quote))
        buys.sort(key=lambda item: item[1], reverse=True)
        for ticker, need in buys:
            if cash < _MIN_QUOTE:
                break
            quote = min(need, cash)
            if quote < _MIN_QUOTE:
                continue
            try:
                parts.append(self._place_order(ticker, "Buy", quote_usdt=quote, maker=True))
                cash -= quote
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] дозор покупка %s: %s", ticker, exc)
        dip_phrase = self._trade_btc_dip(
            day_chg=day_chgs.get(BTC_DIP_TICKER),
            positions=positions,
            price=float(prices.get(BTC_DIP_TICKER) or 0),
            blocked=BTC_DIP_TICKER in trail_sold,
            equity=equity,
        )
        if dip_phrase:
            parts.append(dip_phrase)
        if not parts:
            return "дозор: без сделок"
        return "дозор: " + " ".join(parts)

    def _trade_btc_dip(
        self,
        *,
        day_chg: float | None,
        positions: dict[str, dict[str, Any]],
        price: float,
        blocked: bool,
        equity: float = 0.0,
    ) -> str | None:
        """Карман SOL на долю счёта. Уже открытый не добираем, слот рукава не трогаем."""
        ticker = BTC_DIP_TICKER
        dip = read_btc_dip()
        qty = float(dip.get("qty") or 0)
        if qty > 0:
            reason = btc_dip_exit_reason(price=price, entry=float(dip.get("entry") or 0))
            if not reason or price <= 0:
                return None
            held = float((positions.get(ticker) or {}).get("qty") or 0)
            sell_qty = min(qty, held)
            if sell_qty <= 0:
                write_btc_dip(0, 0)
                return None
            phrase = self._place_order(ticker, "Sell", base_qty=sell_qty, price=price, maker=True)
            write_btc_dip(0, 0)
            logger.info(
                "[Крипта] карман %s: выход (%s) %.6g по %.4g",
                ticker,
                reason,
                sell_qty,
                price,
            )
            return phrase
        if blocked or not btc_dip_should_buy(day_chg=day_chg, qty=0):
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
            write_btc_dip(bought, quote / bought)
            logger.info("[Крипта] карман %s: купил на %.0f$ по просадке суток", ticker, quote)
        return phrase

    def _rebalance(self, target_alloc: dict[str, float], *, respect_hold: bool = True) -> str:
        for ticker in list(target_alloc):
            if ticker not in DESK_ALT_SLEEVE:
                target_alloc.pop(ticker, None)
        cash, positions_list = self._wallet()
        positions = {item["ticker"]: item for item in positions_list}
        self._ensure_desk_bought(set(positions.keys()))
        equity = max(cash + sum(item["value"] for item in positions_list), 1.0)
        min_trade = _min_trade_usd(equity)
        prices = {item["ticker"]: float(item["price"] or 0) for item in positions_list}
        day_chgs: dict[str, float | None] = {}
        for ticker in set(target_alloc) | set(positions):
            need_px = ticker not in prices or prices[ticker] <= 0
            try:
                px = self._ticker(ticker)
                if need_px:
                    prices[ticker] = float(px.get("price") or 0)
                day_chgs[ticker] = float(px.get("chg") or 0)
            except Exception:
                if need_px:
                    prices[ticker] = 0.0
                day_chgs[ticker] = None
        relevant = set(positions) | set(target_alloc)
        target_values = {ticker: equity * (target_alloc.get(ticker, 0.0) / 100.0) for ticker in relevant}
        current_values = {
            ticker: float(positions.get(ticker, {}).get("value") or 0)
            for ticker in relevant
        }
        dip_qty = float(read_btc_dip().get("qty") or 0)
        dip_px = float(prices.get(BTC_DIP_TICKER) or 0)
        if dip_qty > 0 and dip_px > 0 and respect_hold and BTC_DIP_TICKER in current_values:
            # Карман не продаём как лишний альт и не занимаем им слот ядра.
            current_values[BTC_DIP_TICKER] = max(
                0.0, current_values[BTC_DIP_TICKER] - dip_qty * dip_px
            )
        parts: list[str] = []
        sells = [
            (ticker, current_values[ticker] - target_values[ticker])
            for ticker in relevant
            if current_values[ticker] > target_values[ticker]
            and should_rebalance_leg(
                current_value=current_values[ticker],
                target_value=target_values[ticker],
                equity=equity,
                band_pct=band_pct_for(ticker),
                min_trade_usd=min_trade,
                day_chg=None if ticker in DESK_ALT_SLEEVE else day_chgs.get(ticker),
                take_profit_pct=take_profit_day_pct_for(ticker),
            )
        ]
        sells.sort(key=lambda item: item[1], reverse=True)
        fresh: set[str] = set()
        if respect_hold:
            try:
                fresh = crypto_journal.recently_bought(ALT_MIN_HOLD_HOURS)
            except Exception:
                fresh = set()
        kept_alt_value = 0.0
        for ticker, excess in sells:
            if ticker not in DESK_ALT_SLEEVE:
                continue
            if target_alloc.get(ticker, 0.0) <= 0 and self._is_owner_position(ticker):
                continue
            if ticker in fresh and ticker in DESK_ALT_SLEEVE:
                kept_alt_value += excess
                logger.info(
                    "[Крипта] %s куплен < %d ч назад — не ротирую (выход только у средней или стоп)",
                    ticker,
                    int(ALT_MIN_HOLD_HOURS),
                )
                continue
            price = prices.get(ticker) or 0.0
            qty = float(positions.get(ticker, {}).get("qty") or 0)
            if ticker == BTC_DIP_TICKER and dip_qty > 0 and respect_hold:
                qty = max(0.0, qty - dip_qty)
            if target_alloc.get(ticker, 0.0) <= 0:
                sell_qty = qty
            else:
                sell_qty = excess / price if price else 0.0
            sell_qty = min(sell_qty, qty)
            if sell_qty <= 0:
                continue
            try:
                parts.append(self._place_order(ticker, "Sell", base_qty=sell_qty, price=price, maker=True))
                if ticker == BTC_DIP_TICKER and not respect_hold:
                    write_btc_dip(0, 0)
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] продажа %s: %s", ticker, exc)
        time.sleep(0.3)
        self._bust_private_cache()
        cash, _pos = self._cash_and_held()
        budget = cash * common._BUY_CASH_BUFFER
        reserve = pocket_cash_reserve(
            equity, pocket_open=float(read_btc_dip().get("qty") or 0) > 0
        )
        cash = max(0.0, budget - reserve)
        buys = [
            (ticker, target_values.get(ticker, 0) - current_values.get(ticker, 0))
            for ticker in target_alloc
            if should_rebalance_leg(
                current_value=current_values.get(ticker, 0),
                target_value=target_values.get(ticker, 0),
                equity=equity,
                band_pct=band_pct_for(ticker),
                min_trade_usd=min_trade,
                day_chg=day_chgs.get(ticker),
                take_profit_pct=take_profit_day_pct_for(ticker),
            )
            and target_values.get(ticker, 0) > current_values.get(ticker, 0)
        ]
        # Удержанный свежий альт занимает место в рукаве — новые альты добираем на остаток.
        if kept_alt_value > 0:
            alt_need = sum(n for t, n in buys if t in DESK_ALT_SLEEVE and n > 0)
            if alt_need > 0:
                scale = max(0.0, alt_need - kept_alt_value) / alt_need
                buys = [(t, n * scale if t in DESK_ALT_SLEEVE else n) for t, n in buys]
        buys = [(t, n) for t, n in buys if n > 0]
        buys.sort(key=lambda item: item[1], reverse=True)
        need_sum = sum(need for _ticker, need in buys)
        for ticker, need in buys:
            if ticker not in DESK_ALT_SLEEVE:
                continue
            if cash < _MIN_QUOTE:
                break
            if need_sum > budget + 1e-9:
                quote = budget * (need / need_sum)
            else:
                quote = need
            quote = min(quote, cash, need)
            if quote < _MIN_QUOTE:
                continue
            try:
                parts.append(self._place_order(ticker, "Buy", quote_usdt=quote, maker=True))
                cash -= quote
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] покупка %s: %s", ticker, exc)
        if not parts:
            return "Портфель уже в коридоре цели."
        return " ".join(parts)

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
        self._desk_stop.wait(90)
        next_desk = 0.0
        next_watch = 0.0
        while not self._desk_stop.is_set():
            if not self._desk_ready():
                if self._desk_stop.wait(2.0):
                    return
                continue
            now = time.time()
            if next_desk <= 0:
                next_desk = next_desk_at(read_alloc_state(), now)
                next_watch = now + _WATCH_PERIOD_SEC
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
            now = time.time()
            try:
                self._reload_env()
                if now >= next_desk:
                    self._trade_auto(silent=True)
                    self._maybe_daily_report()
                    next_desk = time.time() + _DESK_PERIOD_SEC
                    next_watch = time.time() + _WATCH_PERIOD_SEC
                elif now >= next_watch:
                    self._desk_watch(silent=True)
                    next_watch = time.time() + _WATCH_PERIOD_SEC
            except Exception as exc:
                logger.warning("[Крипта] фоновый цикл: %s", exc)
                next_watch = time.time() + _WATCH_PERIOD_SEC
