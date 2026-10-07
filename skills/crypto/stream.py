# skills/crypto/stream.py
# Публичная лента Bybit: тикеры держанных ног и закрытие свечи 4h.
# Ордера и кошелёк по-прежнему REST.

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .common import _QUOTE, _testnet
from .desk_policy import WATCH_PERIOD_CALM, watch_period_sec

logger = logging.getLogger(__name__)

_WS_PROD = "wss://stream.bybit.com/v5/public/spot"
_WS_TEST = "wss://stream-testnet.bybit.com/v5/public/spot"
_QUOTE_MAX_AGE = 15.0
_STALE_SEC = 40.0


def _ws_url(testnet: bool = False) -> str:
    return _WS_TEST if testnet else _WS_PROD


def stream_topics(tickers: set[str] | list[str]) -> list[str]:
    topics: list[str] = []
    for raw in sorted({str(t).upper() for t in tickers if t}):
        symbol = f"{raw}{_QUOTE}"
        topics.append(f"tickers.{symbol}")
        topics.append(f"kline.240.{symbol}")
    return topics


class BybitPublicTape:
    """Демон-сокет. Тесты кормят apply_message без сети."""

    def __init__(self, *, testnet: bool | None = None) -> None:
        self._testnet = _testnet() if testnet is None else bool(testnet)
        self._lock = threading.Lock()
        self._px: dict[str, dict[str, Any]] = {}
        self._wanted: set[str] = set()
        self._legs: list[dict[str, Any]] = []
        self._hot_tickers: set[str] = set()
        self._wake = threading.Event()
        self._kline = threading.Event()
        self._signal = threading.Event()
        self._stop = threading.Event()
        self._last_msg = 0.0
        self._ws: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.warning("[Крипта] лента: нет websocket-client — только REST")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bybit-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._signal.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def wait(self, timeout: float) -> bool:
        return self._signal.wait(max(0.0, float(timeout)))

    def consume_signals(self) -> str:
        bits: list[str] = []
        if self._wake.is_set():
            self._wake.clear()
            bits.append("зона")
        if self._kline.is_set():
            self._kline.clear()
            bits.append("свеча")
        self._signal.clear()
        return "+".join(bits)

    def set_symbols(self, tickers: set[str] | list[str]) -> None:
        wanted = {str(t).upper() for t in tickers if t}
        with self._lock:
            old = set(self._wanted)
            self._wanted = wanted
        added = wanted - old
        dropped = old - wanted
        if not added and not dropped:
            return
        logger.info("[Крипта] лента: %s", ", ".join(sorted(wanted)) or "пусто")
        ws = self._ws
        if ws is None:
            return
        if added:
            self._send(ws, "subscribe", stream_topics(added))
        if dropped:
            self._send(ws, "unsubscribe", stream_topics(dropped))

    def set_legs(self, legs: list[dict[str, Any]]) -> None:
        clean = [dict(leg) for leg in legs if str(leg.get("ticker") or "").strip()]
        with self._lock:
            self._legs = clean
            self._hot_tickers = self._hot_now_locked()

    def quote(self, ticker: str, *, max_age: float = _QUOTE_MAX_AGE) -> dict[str, float] | None:
        ticker = str(ticker or "").upper()
        with self._lock:
            row = self._px.get(ticker)
            if not row:
                return None
            ts = float(row.get("ts") or 0)
            price = float(row.get("price") or 0)
            if price <= 0 or ts <= 0 or time.time() - ts > max_age:
                return None
            chg = row.get("chg")
            out: dict[str, float] = {"price": price, "ts": ts}
            if chg is not None:
                out["chg"] = float(chg)
            return out

    def is_alive(self, *, max_age: float = _STALE_SEC) -> bool:
        if self._last_msg <= 0:
            return False
        return (time.time() - self._last_msg) < max_age

    def apply_message(self, raw: str | dict[str, Any]) -> None:
        msg = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(msg, dict):
            return
        op = str(msg.get("op") or "")
        if op == "ping":
            ws = self._ws
            if ws is not None:
                self._send(ws, "pong", None)
            return
        topic = str(msg.get("topic") or "")
        data = msg.get("data")
        if topic.startswith("tickers.") and isinstance(data, dict):
            self._on_ticker(topic, data)
            return
        if topic.startswith("kline.240.") and isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else {}
            confirm = row.get("confirm")
            if confirm is True or str(confirm).lower() == "true":
                self._kline.set()
                self._signal.set()
                logger.info("[Крипта] лента: закрылась 4h %s", topic.split(".")[-1])

    def _on_ticker(self, topic: str, data: dict[str, Any]) -> None:
        symbol = str(data.get("symbol") or topic.split(".", 1)[-1] or "").upper()
        if symbol.endswith(_QUOTE):
            ticker = symbol[: -len(_QUOTE)]
        else:
            ticker = symbol
        if not ticker:
            return
        with self._lock:
            prev = self._px.get(ticker) or {}
            last = data.get("lastPrice")
            price = float(last) if last not in (None, "") else float(prev.get("price") or 0)
            if price <= 0:
                return
            chg = prev.get("chg")
            raw_chg = data.get("price24hPcnt")
            if raw_chg not in (None, ""):
                chg = float(raw_chg) * 100.0
            self._px[ticker] = {
                "price": price,
                "chg": chg,
                "bid": float(data.get("bid1Price") or prev.get("bid") or 0),
                "ask": float(data.get("ask1Price") or prev.get("ask") or 0),
                "ts": time.time(),
            }
            self._last_msg = time.time()
            entered = self._mark_hot_locked(ticker, price)
        if entered:
            logger.info("[Крипта] лента: зона %s", ",".join(sorted(entered)))
            self._wake.set()
            self._signal.set()

    def _hot_now_locked(self) -> set[str]:
        hot: set[str] = set()
        for leg in self._legs:
            ticker = str(leg.get("ticker") or "").upper()
            row = self._px.get(ticker) or {}
            price = float(row.get("price") or leg.get("price") or 0)
            if self._leg_hot(leg, price):
                hot.add(ticker)
        return hot

    def _mark_hot_locked(self, ticker: str, price: float) -> set[str]:
        now_hot: set[str] = set()
        for leg in self._legs:
            name = str(leg.get("ticker") or "").upper()
            px = price if name == ticker else float((self._px.get(name) or {}).get("price") or 0)
            if self._leg_hot(leg, px):
                now_hot.add(name)
        entered = now_hot - self._hot_tickers
        self._hot_tickers = now_hot
        return entered

    @staticmethod
    def _leg_hot(leg: dict[str, Any], price: float) -> bool:
        if price <= 0:
            return False
        row = dict(leg)
        row["price"] = price
        return watch_period_sec(legs=[row]) < WATCH_PERIOD_CALM

    def _run(self) -> None:
        websocket = __import__("websocket")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    _ws_url(self._testnet),
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning("[Крипта] лента: %s", exc)
            self._ws = None
            if self._stop.is_set():
                return
            logger.warning("[Крипта] лента: переподключение через %.0fс", backoff)
            if self._stop.wait(backoff):
                return
            backoff = min(30.0, backoff * 2)

    def _on_open(self, ws: Any) -> None:
        logger.info("[Крипта] лента: сокет открыт")
        with self._lock:
            wanted = set(self._wanted)
        if wanted:
            self._send(ws, "subscribe", stream_topics(wanted))

    def _on_message(self, _ws: Any, raw: str) -> None:
        try:
            self.apply_message(raw)
        except Exception as exc:
            logger.info("[Крипта] лента сообщение: %s", exc)

    def _on_error(self, _ws: Any, exc: BaseException) -> None:
        if self._stop.is_set():
            return
        logger.warning("[Крипта] лента: %s", exc)

    def _on_close(self, _ws: Any, status: Any, msg: Any) -> None:
        if self._stop.is_set():
            return
        logger.info("[Крипта] лента: сокет закрыт %s %s", status or "", msg or "")

    @staticmethod
    def _send(ws: Any, op: str, args: list[str] | None) -> None:
        payload: dict[str, Any] = {"op": op}
        if args:
            payload["args"] = args
        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            logger.info("[Крипта] лента send: %s", exc)


__all__ = ["BybitPublicTape", "stream_topics"]
