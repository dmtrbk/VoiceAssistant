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
    _min_trade_usd,
    _read_ticker_set,
    _spoken,
    _write_ticker_set,
    parse_ai_alloc,
)
from .desk_policy import (
    CHURN_COOLDOWN_HOURS,
    DESK_CORE,
    band_pct_for,
    cash_floor_pct,
    filter_auto_candidates,
    is_risk_off,
    score_alloc,
    should_rebalance_leg,
)
from . import journal as crypto_journal

logger = logging.getLogger(__name__)


def _wants_advice(text: str) -> bool:
    return any(hint in text for hint in _ADVICE_HINTS)


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
        if self._api_key:
            try:
                _cash, positions = self._wallet()
                held = {item["ticker"]: item["qty"] for item in positions}
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
            chosen.append({**row, "held": held.get(ticker, 0), **mom})

        for ticker in list(held) + list(self._watchlist):
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
            if len(chosen) >= 12:
                break
            add(row)
        return chosen

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
            watchlist=list(self._watchlist) + list(DESK_CORE),
            cooldown=cool,
        )

    def desk_score_alloc(self, candidates: list[dict[str, Any]] | None = None) -> tuple[dict[str, float], str]:
        btc7, btc_day = self._btc_regime()
        if is_risk_off(btc_chg_7=btc7, btc_chg_day=btc_day):
            why = (
                f"Риск-офф по BTC (7д {btc7:+.1f}%, сутки {btc_day:+.1f}%) — кэш."
                if btc7 is not None and btc_day is not None
                else "Риск-офф по BTC — держу кэш."
            )
            return {}, why
        rows = candidates if candidates is not None else self.desk_auto_candidates()
        if not rows:
            return {}, "Нет кандидатов для автостола."
        floor = cash_floor_pct(btc_chg_7=btc7, btc_chg_day=btc_day)
        return score_alloc(rows, cash_floor_pct=floor)

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
                            "Если картина плохая — пустой alloc. Не обещай прибыль. Мужской род."
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
        lines = [f"Портфель: {book}", "Кандидаты спот USDT:"]
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
            return self._trade_auto_locked(silent)

    def _trade_auto_locked(self, silent: bool = False) -> str:
        # Авто и «поторгуй» — скор + режим BTC, без Groq.
        candidates = self.desk_auto_candidates()
        alloc, why = self.desk_score_alloc(candidates)
        if not alloc:
            result = why or "Стол оставил кэш."
            if silent:
                logger.info("[Крипта] авто: %s", result)
            return result
        desc = ", ".join(f"{_spoken(ticker)} {int(round(pct))}%" for ticker, pct in alloc.items())
        logger.info("[Крипта] целевой портфель: %s (%s)", desc, why)
        if not self._api_key:
            return f"Целевой портфель: {desc}. Ключа Bybit нет. {why}".strip()
        result = self._rebalance(alloc)
        if why and not silent:
            return f"{result} {why}".strip()
        return result

    def _rebalance(self, target_alloc: dict[str, float]) -> str:
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
        parts: list[str] = []
        sells = [
            (ticker, current_values[ticker] - target_values[ticker])
            for ticker in relevant
            if should_rebalance_leg(
                current_value=current_values[ticker],
                target_value=target_values[ticker],
                equity=equity,
                band_pct=band_pct_for(ticker),
                min_trade_usd=min_trade,
                day_chg=day_chgs.get(ticker),
            )
            and current_values[ticker] > target_values[ticker]
        ]
        sells.sort(key=lambda item: item[1], reverse=True)
        for ticker, excess in sells:
            if target_alloc.get(ticker, 0.0) <= 0 and self._is_owner_position(ticker):
                continue
            price = prices.get(ticker) or 0.0
            qty = float(positions.get(ticker, {}).get("qty") or 0)
            if target_alloc.get(ticker, 0.0) <= 0:
                sell_qty = qty
            else:
                sell_qty = excess / price if price else 0.0
            sell_qty = min(sell_qty, qty)
            if sell_qty <= 0:
                continue
            try:
                parts.append(self._place_order(ticker, "Sell", base_qty=sell_qty, price=price))
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("[Крипта] продажа %s: %s", ticker, exc)
        time.sleep(0.3)
        self._bust_private_cache()
        cash, _pos = self._cash_and_held()
        budget = cash * common._BUY_CASH_BUFFER
        cash = budget
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
            )
            and target_values.get(ticker, 0) > current_values.get(ticker, 0)
        ]
        buys = [(t, n) for t, n in buys if n > 0]
        buys.sort(key=lambda item: item[1], reverse=True)
        need_sum = sum(need for _ticker, need in buys)
        for ticker, need in buys:
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
                parts.append(self._place_order(ticker, "Buy", quote_usdt=quote))
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
            text = crypto_journal.format_daily_pnl_report()
            common.send_telegram_notification(text, background=False)
            crypto_journal.mark_daily_report_sent()
            logger.info("[Крипта] дневной отчёт отправлен.")
        except Exception as exc:
            logger.warning("[Крипта] дневной отчёт: %s", exc)

    def _desk_loop(self) -> None:
        self._desk_stop.wait(90)
        while not self._desk_stop.is_set():
            if not self._desk_ready():
                if self._desk_stop.wait(2.0):
                    return
                continue
            deadline = time.time() + _DESK_PERIOD_SEC
            skipped = False
            while time.time() < deadline:
                remaining = min(2.0, deadline - time.time())
                if remaining <= 0:
                    break
                if self._desk_stop.wait(remaining):
                    return
                if not self._desk_ready():
                    skipped = True
                    break
            if skipped:
                continue
            try:
                self._reload_env()
                self._trade_auto(silent=True)
                self._maybe_daily_report()
            except Exception as exc:
                logger.warning("[Крипта] фоновый цикл: %s", exc)
