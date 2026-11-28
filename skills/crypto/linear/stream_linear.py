"""Публичный WS Bybit v5 linear: kline.15 confirm + trades + bbo."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_WS_PROD = "wss://stream.bybit.com/v5/public/linear"
_WS_TEST = "wss://stream-testnet.bybit.com/v5/public/linear"


class LinearPublicTape:
    def __init__(self, *, testnet: bool = False) -> None:
        self.testnet = testnet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wanted: set[str] = set()
        self.bbo: dict[str, dict[str, float]] = {}
        self.last: dict[str, float] = {}
        self.funding: dict[str, float] = {}
        self._on_kline: Callable[[str, dict[str, Any]], None] | None = None
        self._on_trade: Callable[[str, float, int], None] | None = None
        self._kline_wake = threading.Event()
        self._kline_lock = threading.Lock()
        self.closed_klines: list[tuple[str, dict[str, Any]]] = []

    def set_handlers(
        self,
        *,
        on_kline_closed: Callable[[str, dict[str, Any]], None] | None = None,
        on_trade: Callable[[str, float, int], None] | None = None,
    ) -> None:
        self._on_kline = on_kline_closed
        self._on_trade = on_trade

    def set_symbols(self, symbols: set[str] | list[str]) -> None:
        self._wanted = {s.upper() for s in symbols if s}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.warning("[linear] нет websocket-client — REST-only confirm")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="linear-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait_kline(self, timeout: float = 1.0) -> bool:
        ok = self._kline_wake.wait(timeout)
        self._kline_wake.clear()
        return ok

    def pop_closed(self) -> list[tuple[str, dict[str, Any]]]:
        with self._kline_lock:
            out = list(self.closed_klines)
            self.closed_klines.clear()
            return out

    def _topics(self) -> list[str]:
        topics = []
        for sym in sorted(self._wanted):
            topics.append(f"kline.15.{sym}")
            topics.append(f"publicTrade.{sym}")
            topics.append(f"orderbook.1.{sym}")
            topics.append(f"tickers.{sym}")
        return topics

    def _run(self) -> None:
        import websocket

        url = _WS_TEST if self.testnet else _WS_PROD

        def on_msg(_ws, message: str) -> None:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            topic = str(data.get("topic") or "")
            if topic.startswith("kline.15."):
                sym = topic.split(".")[-1].upper()
                for row in data.get("data") or []:
                    if not bool(row.get("confirm")):
                        continue
                    bar = {
                        "ts": int(row.get("start") or 0),
                        "open": float(row.get("open") or 0),
                        "high": float(row.get("high") or 0),
                        "low": float(row.get("low") or 0),
                        "close": float(row.get("close") or 0),
                        "volume": float(row.get("volume") or 0),
                        "confirm": True,
                    }
                    # только очередь — обработка в desk main loop (дедуп WS/REST)
                    with self._kline_lock:
                        self.closed_klines.append((sym, bar))
                    self._kline_wake.set()
                    if self._on_kline:
                        self._on_kline(sym, bar)

            elif topic.startswith("publicTrade."):
                sym = topic.split(".")[-1].upper()
                for row in data.get("data") or []:
                    px = float(row.get("p") or row.get("price") or 0)
                    ts = int(row.get("T") or row.get("ts") or 0)
                    if px > 0 and self._on_trade:
                        self._on_trade(sym, px, ts)
            elif topic.startswith("orderbook.1."):
                sym = topic.split(".")[-1].upper()
                payload = data.get("data") or {}
                bids = payload.get("b") or []
                asks = payload.get("a") or []
                bid = float(bids[0][0]) if bids else 0.0
                ask = float(asks[0][0]) if asks else 0.0
                self.bbo[sym] = {"bid": bid, "ask": ask}
            elif topic.startswith("tickers."):
                sym = topic.split(".")[-1].upper()
                payload = data.get("data") or {}
                if isinstance(payload, list):
                    payload = payload[0] if payload else {}
                last = float(payload.get("lastPrice") or 0)
                if last:
                    self.last[sym] = last
                fr = payload.get("fundingRate")
                if fr is not None and fr != "":
                    self.funding[sym] = float(fr)

        def on_open(ws) -> None:
            topics = self._topics()
            if topics:
                ws.send(json.dumps({"op": "subscribe", "args": topics}))
                logger.info("[linear] WS subscribed %d topics", len(topics))

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(url, on_message=on_msg, on_open=on_open)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning("[linear] WS reconnect: %s", exc)
            time.sleep(2.0)
