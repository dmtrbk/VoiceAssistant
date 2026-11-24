"""Тонкий REST-клиент Bybit v5 для linear (публичный + signed)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import requests

from skills.crypto.common import _API_PROD, _API_TEST, _testnet

logger = logging.getLogger(__name__)


class BybitRest:
    def __init__(self, *, testnet: bool | None = None, api_key: str = "", api_secret: str = "") -> None:
        self.testnet = _testnet() if testnet is None else bool(testnet)
        self.api_key = api_key or (os.getenv("BYBIT_API_KEY") or os.getenv("BYBIT_KEY") or "")
        self.api_secret = api_secret or (os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET") or "")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "VoiceAssistant-linear/1.0"})

    @property
    def host(self) -> str:
        return _API_TEST if self.testnet else _API_PROD

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.session.get(f"{self.host}{path}", params=params or {}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if int(data.get("retCode") or 0) != 0:
            raise RuntimeError(f"{path} {data.get('retCode')} {data.get('retMsg')}")
        return data

    def signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BYBIT_API_KEY/SECRET required for signed calls")
        params = dict(params or {})
        ts = str(int(time.time() * 1000))
        recv = "5000"
        if method.upper() == "GET":
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
            payload = f"{ts}{self.api_key}{recv}{query}"
            sign = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
            }
            r = self.session.get(f"{self.host}{path}", params=params, headers=headers, timeout=30)
        else:
            body = json.dumps(params, separators=(",", ":"))
            payload = f"{ts}{self.api_key}{recv}{body}"
            sign = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
                "Content-Type": "application/json",
            }
            r = self.session.post(f"{self.host}{path}", data=body, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if int(data.get("retCode") or 0) != 0:
            raise RuntimeError(f"{path} {data.get('retCode')} {data.get('retMsg')}")
        return data
