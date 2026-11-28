"""Приватный WS Bybit v5: order + execution → мгновенный protect после филла."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_WS_PROD = "wss://stream.bybit.com/v5/private"
_WS_TEST = "wss://stream-testnet.bybit.com/v5/private"


class LinearPrivateTape:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_order: Callable[[dict[str, Any]], None] | None = None
        self._on_execution: Callable[[dict[str, Any]], None] | None = None

    def set_handlers(
        self,
        *,
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_execution: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._on_order = on_order
        self._on_execution = on_execution

    def start(self) -> None:
        if not self.api_key or not self.api_secret:
            logger.warning("[linear] private WS: нет API key/secret — только REST reconcile")
            return
        if self._thread and self._thread.is_alive():
            return
        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.warning("[linear] нет websocket-client — private WS выкл")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="linear-ws-priv")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _auth_args(self) -> list[Any]:
        expires = int((time.time() + 10) * 1000)
        payload = f"GET/realtime{expires}"
        sign = hmac.new(
            self.api_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return [self.api_key, expires, sign]

    def _run(self) -> None:
        import websocket

        url = _WS_TEST if self.testnet else _WS_PROD

        def on_msg(_ws, message: str) -> None:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            topic = str(data.get("topic") or "")
            rows = data.get("data")
            if rows is None:
                return
            if not isinstance(rows, list):
                rows = [rows]
            if topic == "order":
                for row in rows:
                    if isinstance(row, dict) and self._on_order:
                        try:
                            self._on_order(row)
                        except Exception:
                            logger.exception("[linear] private on_order failed")
            elif topic == "execution":
                for row in rows:
                    if isinstance(row, dict) and self._on_execution:
                        try:
                            self._on_execution(row)
                        except Exception:
                            logger.exception("[linear] private on_execution failed")

        def on_open(ws) -> None:
            ws.send(json.dumps({"op": "auth", "args": self._auth_args()}))
            ws.send(json.dumps({"op": "subscribe", "args": ["order", "execution"]}))
            logger.info("[linear] private WS subscribed order+execution")

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(url, on_message=on_msg, on_open=on_open)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning("[linear] private WS reconnect: %s", exc)
            time.sleep(2.0)
