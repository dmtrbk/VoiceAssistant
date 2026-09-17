# skills/stocks/quotes.py
# Котировки ISS / Т-Инвест, книжка, портфель и голосовые сводки.

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from skills.text_utils import norm as _norm
from . import common
from .common import (
    _ALIASES,
    _API_PROD,
    _API_SANDBOX,
    _BOOK_PATH,
    _BY_ID,
    _CACHE_SEC,
    _ENCYCLOPEDIA,
    _FIND,
    _MARKET_WORDS,
    _MOEX,
    _MOEX_BOARDS,
    _MOEX_HISTORY,
    _PORTFOLIO,
    _PRICE_HINTS,
    _PRICES,
    _SPOKEN,
    _STATUS_ASK,
    _USERS,
    _format_pct,
    _format_rub,
    _http_error,
    _is_api_buy_forbidden_text,
    _quotation_to_float,
    _sandbox,
    _tinkoff_verify,
)

logger = logging.getLogger(__name__)

# «Зачем тебе биржа» — характер, не сводка. Короткое «биржа» / «акции» — сводка.
_IDENTITY_TALK = (
    "зачем",
    "почему ты",
    "почему тебе",
    "для чего",
    "любишь",
    "предпочита",
    "смысл бирж",
    "зачем тебе",
    "зачем ты",
)


def _is_identity_talk(text: str) -> bool:
    return any(hint in text for hint in _IDENTITY_TALK)


def _wants_market_report(text: str) -> bool:
    """Сводка по счёту, а не «что такое акции» и не «зачем тебе биржа»."""
    if any(phrase in text for phrase in _ENCYCLOPEDIA):
        return False
    if _is_identity_talk(text):
        return False
    if not any(word in text for word in _MARKET_WORDS):
        return False
    if any(ask in text for ask in _STATUS_ASK):
        return True
    return len(text.split()) <= 3



class StocksQuotesMixin:
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, service_method: str) -> str:
        root = _API_SANDBOX if _sandbox() else _API_PROD
        return f"{root}/tinkoff.public.invest.api.contract.v1.{service_method}"

    def _bust_broker_cache(self) -> None:
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if not key.startswith(("accounts", "pf:", "px:", "figi:", "find:", "max:"))
        }

    def _post(self, url: str, body: dict[str, Any], cache_key: str | None = None) -> dict[str, Any]:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit and time.time() - hit[0] < _CACHE_SEC:
                return hit[1]
        response = self._session.post(
            url,
            json=body,
            headers=self._headers(),
            timeout=6,
            verify=_tinkoff_verify(),
        )
        if response.status_code == 401:
            raise RuntimeError("token rejected")
        if response.status_code >= 400:
            snippet = (response.text or "")[:240].lower()
            if response.status_code in {403, 400} and any(
                marker in snippet
                for marker in ("permission", "прав", "readonly", "read only", "недостаточно прав")
            ):
                raise RuntimeError("trade token")
            if _is_api_buy_forbidden_text(snippet):
                raise RuntimeError("api forbidden")
            raise _http_error(response)
        data = response.json() if response.content else {}
        if cache_key:
            self._cache[cache_key] = (time.time(), data)
        return data

    def _get(self, url: str, params: dict[str, str], cache_key: str) -> dict[str, Any]:
        hit = self._cache.get(cache_key)
        if hit and time.time() - hit[0] < _CACHE_SEC:
            return hit[1]
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = self._session.get(url, params=params, timeout=6)
                if response.status_code >= 400:
                    raise _http_error(response)
                data = response.json() if response.content else {}
                self._cache[cache_key] = (time.time(), data)
                return data
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                time.sleep(0.4)
        if last_error:
            raise last_error
        raise RuntimeError("moex get failed")

    def _iss_rows(self, block: Any) -> list[dict[str, Any]]:
        if not isinstance(block, dict):
            return []
        cols = block.get("columns") or []
        return [dict(zip(cols, row)) for row in (block.get("data") or [])]

    def _load_book(self) -> list[dict[str, Any]]:
        # Локальная книжка бумаг без токена. Не коммитить (см. .gitignore).
        if not os.path.exists(_BOOK_PATH):
            return []
        try:
            with open(_BOOK_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            logger.warning("[Биржа] quiet_book.json: %s", exc)
            return []
        items = raw.get("positions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        book: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            book.append(
                {
                    "ticker": ticker,
                    "qty": float(item.get("qty") or 0),
                    "avg": float(item.get("avg") or 0),
                }
            )
        return book

    def _book_cash(self) -> float:
        if not os.path.exists(_BOOK_PATH):
            return 0.0
        try:
            with open(_BOOK_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return 0.0
        if isinstance(raw, dict):
            try:
                return float(raw.get("cash") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _broker_cash(self) -> float:
        if not self._token:
            return self._book_cash()
        account_id = self._pick_account_id()
        if not account_id:
            return 0.0
        try:
            data = self._post(
                self._url(_PORTFOLIO),
                {"accountId": account_id, "currency": "RUB"},
                cache_key=f"pf:{account_id}",
            )
            cash_obj = data.get("totalAmountCurrencies") or {}
            cash = _quotation_to_float(cash_obj)
            # С токеном нулевой кэш брокера — это ноль, а не устаревшая quiet_book.
            return max(cash, 0.0)
        except Exception as exc:
            logger.warning("[Биржа] Не удалось получить кэш брокера: %s", exc)
            return 0.0

    def _watch_tickers(self) -> list[str]:
        ordered: list[str] = []
        for item in self._load_book():
            ticker = item["ticker"]
            if ticker not in ordered:
                ordered.append(ticker)
        for ticker in self._watchlist:
            if ticker not in ordered:
                ordered.append(ticker)
        return ordered

    def _pick_marketdata_row(self, rows: list[dict[str, Any]]) -> tuple[float, float] | None:
        preferred = []
        others = []
        for row in rows:
            board = str(row.get("BOARDID") or "")
            last = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE")
            if last in (None, "", 0, 0.0):
                continue
            bucket = preferred if board in _MOEX_BOARDS else others
            bucket.append((float(last), float(row.get("LASTTOPREVPRICE") or 0)))
        chosen = preferred or others
        return chosen[0] if chosen else None

    def _moex_quote(self, ticker: str) -> tuple[str, float, float]:
        ticker = ticker.upper()
        last_error: Exception | None = None
        urls = [f"{_MOEX}/boards/{board}/securities/{ticker}.json" for board in _MOEX_BOARDS]
        urls.append(f"{_MOEX}/securities/{ticker}.json")
        for url in urls:
            try:
                data = self._get(
                    url,
                    {"iss.meta": "off", "iss.only": "marketdata"},
                    f"moex:{url}",
                )
            except Exception as exc:
                last_error = exc
                logger.debug("[Биржа] MOEX %s: %s", url, exc)
                continue
            picked = self._pick_marketdata_row(self._iss_rows(data.get("marketdata")))
            if not picked:
                continue
            last, pct = picked
            spoken = self._spoken_name(ticker)
            self._remember_name(ticker, spoken)
            return spoken, last, pct
        if last_error:
            raise last_error
        raise RuntimeError(f"no moex quote {ticker}")

    def _closes_history(self, ticker: str, limit: int = 16) -> list[float]:
        ticker = ticker.upper()
        for board in _MOEX_BOARDS:
            try:
                data = self._get(
                    f"{_MOEX_HISTORY}/boards/{board}/securities/{ticker}.json",
                    {
                        "iss.meta": "off",
                        "iss.only": "history",
                        "sort_order": "desc",
                        "limit": str(limit),
                    },
                    f"hist:{board}:{ticker}",
                )
            except Exception as exc:
                logger.debug("[Биржа] история %s %s: %s", board, ticker, exc)
                continue
            rows = self._iss_rows(data.get("history"))
            rows.sort(key=lambda row: str(row.get("TRADEDATE") or ""), reverse=True)
            closes: list[float] = []
            for row in rows:
                close = row.get("CLOSE") or row.get("LEGALCLOSEPRICE")
                if close in (None, "", 0, 0.0):
                    continue
                closes.append(float(close))
            if len(closes) >= 11:
                return closes
            if closes:
                return closes
        return []

    def _momentum_10d(self, ticker: str) -> dict[str, Any]:
        closes = self._closes_history(ticker)
        if len(closes) < 11:
            now = closes[0] if closes else 0.0
            return {"close": now, "close_10": 0.0, "chg_10": None, "trend": "нет данных"}
        now, then = closes[0], closes[10]
        if then <= 0:
            return {"close": now, "close_10": then, "chg_10": None, "trend": "нет данных"}
        chg = round((now / then - 1.0) * 100.0, 1)
        if now > then:
            trend = "выше"
        elif now < then:
            trend = "ниже"
        else:
            trend = "как 10д назад"
        return {"close": now, "close_10": then, "chg_10": chg, "trend": trend}

    def _quote(self, ticker: str) -> tuple[str, float, float]:
        if self._token:
            try:
                name, price = self._tinkoff_last(ticker)
                return name, price, 0.0
            except Exception as exc:
                logger.warning("[Биржа] котировка брокера недоступна: %s", exc)
        return self._moex_quote(ticker)

    def _ticker_from_text(self, text: str) -> str | None:
        aliases = dict(_ALIASES)
        aliases.update(self._alias_extra)
        for name in sorted(aliases, key=len, reverse=True):
            if re.search(rf"(?<![а-яa-z]){re.escape(name)}(?![а-яa-z])", text):
                return aliases[name]
        for ticker in list(self._watchlist) + list(self._last_tickers):
            if ticker and ticker.lower() in text:
                return ticker
        return None

    def _pick_account_id(self) -> str | None:
        if self._account_id:
            return self._account_id
        data = self._post(self._url(_USERS), {}, cache_key="accounts")
        accounts = data.get("accounts") or []
        open_accounts = [
            acc for acc in accounts
            if acc.get("status") in (None, "ACCOUNT_STATUS_OPEN", "ACCOUNT_STATUS_UNSPECIFIED")
        ]
        if not open_accounts:
            open_accounts = accounts
        preferred = (
            "ACCOUNT_TYPE_TINKOFF",
            "ACCOUNT_TYPE_TINKOFF_IIS",
            "ACCOUNT_TYPE_INVEST_BOX",
        )
        for kind in preferred:
            for acc in open_accounts:
                if acc.get("type") == kind and acc.get("id"):
                    return str(acc["id"])
        for acc in open_accounts:
            if acc.get("id"):
                return str(acc["id"])
        return None

    def _instrument(self, figi: str | None, ticker: str | None, uid: str | None = None) -> dict[str, Any]:
        if uid:
            try:
                data = self._post(
                    self._url(_BY_ID),
                    {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid},
                    cache_key=f"uid:{uid}",
                )
                inst = data.get("instrument") or {}
                if inst:
                    return inst
            except Exception as exc:
                logger.debug("[Биржа] инструмент uid %s: %s", uid, exc)
        if figi:
            data = self._post(
                self._url(_BY_ID),
                {"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi},
                cache_key=f"figi:{figi}",
            )
            inst = data.get("instrument") or {}
            if inst:
                return inst
        if ticker:
            ticker_up = ticker.upper()
            for class_code in _MOEX_BOARDS:
                try:
                    data = self._post(
                        self._url(_BY_ID),
                        {
                            "idType": "INSTRUMENT_ID_TYPE_TICKER",
                            "classCode": class_code,
                            "id": ticker_up,
                        },
                        cache_key=f"by:{class_code}:{ticker_up}",
                    )
                except Exception:
                    continue
                inst = data.get("instrument") or {}
                if inst.get("figi") or inst.get("uid") or inst.get("instrumentId"):
                    return inst
            kinds = (
                None,
                "INSTRUMENT_TYPE_SHARE",
                "INSTRUMENT_TYPE_ETF",
            )
            for kind in kinds:
                body: dict[str, Any] = {
                    "query": ticker,
                    "apiTradeAvailableFlag": True,
                }
                if kind:
                    body["instrumentKind"] = kind
                cache = f"find:{ticker}:{kind or 'any'}"
                data = self._post(self._url(_FIND), body, cache_key=cache)
                found = None
                for inst in data.get("instruments") or []:
                    if str(inst.get("ticker") or "").upper() != ticker_up:
                        continue
                    class_code = str(inst.get("classCode") or "")
                    if class_code in ("TQBR", "TQTF", "TQPI", ""):
                        return inst
                    found = inst
                if found:
                    return found
        return {}

    def _remember_name(self, ticker: str, spoken: str) -> None:
        ticker = ticker.upper()
        spoken_key = _norm(spoken)
        if spoken_key:
            self._alias_extra[spoken_key] = ticker
        if ticker not in self._last_tickers:
            self._last_tickers.append(ticker)
            self._last_tickers = self._last_tickers[-8:]

    def _spoken_name(self, ticker: str, instrument: dict[str, Any] | None = None) -> str:
        if ticker in _SPOKEN:
            return _SPOKEN[ticker]
        name = str((instrument or {}).get("name") or "").strip()
        if name:
            return name.split(",")[0].strip()
        return ticker

    def _positions(self) -> tuple[list[dict[str, Any]], float, float]:
        account_id = self._pick_account_id()
        if not account_id:
            raise RuntimeError("no account")
        data = self._post(
            self._url(_PORTFOLIO),
            {"accountId": account_id, "currency": "RUB"},
            cache_key=f"pf:{account_id}",
        )
        positions: list[dict[str, Any]] = []
        for raw in data.get("positions") or []:
            kind = str(raw.get("instrumentType") or "").lower()
            if kind not in {"share", "etf", "instrument_type_share", "instrument_type_etf"}:
                continue
            figi = raw.get("figi")
            ticker = str(raw.get("ticker") or "").upper()
            instrument = self._instrument(figi, ticker or None)
            ticker = ticker or str(instrument.get("ticker") or "").upper()
            if not ticker:
                continue
            qty = _quotation_to_float(raw.get("quantity"))
            price = _quotation_to_float(raw.get("currentPrice"))
            avg = _quotation_to_float(raw.get("averagePositionPrice"))
            expected = _quotation_to_float(raw.get("expectedYield"))
            daily = _quotation_to_float(raw.get("dailyYield"))
            spoken = self._spoken_name(ticker, instrument)
            self._remember_name(ticker, spoken)
            positions.append(
                {
                    "ticker": ticker,
                    "name": spoken,
                    "qty": qty,
                    "price": price,
                    "avg": avg,
                    "yield": expected,
                    "daily": daily,
                }
            )
        day_total = _quotation_to_float(data.get("dailyYield"))
        if not day_total:
            day_total = sum(item["daily"] for item in positions)
        total_yield = _quotation_to_float(data.get("expectedYield"))
        return positions, day_total, total_yield

    def _tinkoff_last(self, ticker: str) -> tuple[str, float]:
        instrument = self._instrument(None, ticker)
        figi = instrument.get("figi")
        if not figi:
            raise RuntimeError(f"unknown ticker {ticker}")
        data = self._post(self._url(_PRICES), {"figi": [figi]}, cache_key=f"px:{figi}")
        prices = data.get("lastPrices") or []
        price = _quotation_to_float(prices[0].get("price")) if prices else 0.0
        spoken = self._spoken_name(ticker, instrument)
        self._remember_name(ticker, spoken)
        return spoken, price

    def _last_price(self, ticker: str) -> tuple[str, float]:
        name, price, _pct = self._quote(ticker)
        return name, price

    def _line(self, item: dict[str, Any]) -> str:
        price = item["price"]
        avg = item["avg"]
        pct = 0.0
        if avg:
            pct = (price - avg) / avg * 100.0
        elif item["yield"] and price and item["qty"]:
            cost = price * item["qty"] - item["yield"]
            if cost:
                pct = item["yield"] / cost * 100.0
        return f"{item['name']} {_format_rub(price)}, {_format_pct(pct)}"

    def _speak_one(self, ticker: str) -> str:
        if self._token:
            positions, day_total, _total_yield = self._safe_positions()
            for item in positions:
                if item["ticker"] == ticker:
                    extra = ""
                    if item["daily"]:
                        extra = f" За день {_format_rub(item['daily'], signed=True)}."
                    return self._line(item) + "." + extra
        book = {item["ticker"]: item for item in self._load_book()}
        name, price, day_pct = self._quote(ticker)
        pos = book.get(ticker) or {}
        avg = float(pos.get("avg") or 0)
        qty = float(pos.get("qty") or 0)
        if avg:
            pnl = (price - avg) * qty if qty else 0.0
            speech = f"{name} {_format_rub(price)}, {_format_pct((price - avg) / avg * 100.0)}."
            if qty:
                speech += f" С покупки {_format_rub(pnl, signed=True)}."
        else:
            speech = f"{name} {_format_rub(price)}, {_format_pct(day_pct)} за день."
        if qty and day_pct:
            daily = qty * price * day_pct / 100.0
            speech += f" За день {_format_rub(daily, signed=True)}."
        return speech

    def _speak_watch(self, emphasize_yield: bool) -> str:
        tickers = self._watch_tickers()
        if not tickers:
            return "Ключа от счёта ещё нет. Назови бумагу — сверю по бирже."
        book = {item["ticker"]: item for item in self._load_book()}
        parts: list[str] = []
        day_total = 0.0
        total_pnl = 0.0
        have_day = False
        have_pnl = False
        for ticker in tickers[:4]:
            name, price, day_pct = self._quote(ticker)
            pos = book.get(ticker) or {}
            avg = float(pos.get("avg") or 0)
            qty = float(pos.get("qty") or 0)
            if avg:
                parts.append(f"{name} {_format_rub(price)}, {_format_pct((price - avg) / avg * 100.0)}")
                if qty:
                    total_pnl += (price - avg) * qty
                    have_pnl = True
            else:
                parts.append(f"{name} {_format_rub(price)}, {_format_pct(day_pct)} за день")
            if qty:
                day_total += qty * price * (day_pct / 100.0)
                have_day = True
        speech = ". ".join(parts) + "."
        if have_pnl:
            speech += f" С покупки {_format_rub(total_pnl, signed=True)}."
        elif emphasize_yield and have_day:
            speech += f" За день {_format_rub(day_total, signed=True)}."
        cash = self._book_cash()
        if cash:
            speech += f" Кэшем {_format_rub(cash)}."
        return speech

    def _speak_portfolio(self, emphasize_yield: bool) -> str:
        positions, day_total, total_yield = self._positions()
        if not positions:
            if self._watchlist:
                parts = []
                for ticker in self._watchlist[:4]:
                    name, price = self._last_price(ticker)
                    parts.append(f"{name} {_format_rub(price)}")
                return "Наблюдаю: " + "; ".join(parts) + "."
            return "На тихом счёте сейчас нет акций."

        lines = [self._line(item) for item in positions[:4]]
        speech = ". ".join(lines) + "."
        if emphasize_yield or len(positions) > 1:
            if day_total:
                speech += f" За день {_format_rub(day_total, signed=True)}."
            elif total_yield:
                speech += f" С покупки {_format_rub(total_yield, signed=True)}."
        return speech

    def _safe_positions(self) -> tuple[list[dict[str, Any]], float, float]:
        try:
            return self._positions()
        except Exception as exc:
            logger.warning("[Биржа] Портфель недоступен: %s", exc)
            return [], 0.0, 0.0

    def _market_open(self) -> bool:
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        main = 10 * 60 <= minutes <= 18 * 60 + 50
        evening = 19 * 60 + 5 <= minutes <= 23 * 60 + 50
        return main or evening
