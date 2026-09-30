# skills/crypto/earn.py
# Простаивающий кэш под проценты Bybit Flexible Saving (USDT).
# Вывод мгновенный, поэтому держим там всё свободное, а перед покупкой возвращаем нужное.

from __future__ import annotations

import logging
import time
from typing import Any

from . import common

logger = logging.getLogger(__name__)

_CATEGORY = "FlexibleSaving"
_PRODUCT_ID = "1"  # USDT Flexible Saving
_MIN_STAKE = 1.5  # минимум продукта
# Оставляем немного свободным: мелкие заявки не гоняют деньги туда-обратно.
_FREE_BUFFER = 5.0
_POSITION_CACHE_SEC = 20.0


class CryptoEarnMixin:
    def _earn_enabled(self) -> bool:
        return bool(self._api_key) and common._earn_enabled()

    def _earn_staked(self, *, fresh: bool = False) -> float:
        """Сколько USDT лежит под процентами. 0 — выключено, нет прав или ошибка."""
        if not self._earn_enabled():
            return 0.0
        hit = self._cache.get("earn:pos")
        if not fresh and hit and time.time() - hit[0] < _POSITION_CACHE_SEC:
            return float(hit[1])
        try:
            data = self._signed(
                "GET", "/v5/earn/position", {"category": _CATEGORY, "coin": "USDT"}
            )
        except Exception as exc:
            logger.info("[Крипта] Earn: позиция недоступна (%s)", exc)
            return 0.0
        total = 0.0
        for row in (data.get("result") or {}).get("list") or []:
            total += float(row.get("amount") or 0)
        self._cache["earn:pos"] = (time.time(), total)
        return total

    def _earn_order(self, order_type: str, amount: float) -> bool:
        body: dict[str, Any] = {
            "category": _CATEGORY,
            "orderType": order_type,
            "accountType": "UNIFIED",
            "amount": f"{amount:.4f}",
            "coin": "USDT",
            "productId": _PRODUCT_ID,
            "orderLinkId": f"jarvis{int(time.time() * 1000)}",
        }
        try:
            self._signed("POST", "/v5/earn/place-order", body)
        except Exception as exc:
            logger.info("[Крипта] Earn %s %.2f$: %s", order_type.lower(), amount, exc)
            return False
        self._cache.pop("earn:pos", None)
        self._bust_private_cache()
        return True

    def _earn_free_cash(self, need: float) -> None:
        """Вернуть со процентов столько, чтобы на споте хватило на need долларов."""
        if not self._earn_enabled() or need <= 0:
            return
        staked = self._earn_staked(fresh=True)
        if staked <= 0:
            return
        try:
            free, _positions = self._wallet(with_earn=False)
        except Exception:
            return
        gap = need - free
        if gap <= 0:
            return
        amount = min(staked, gap + _FREE_BUFFER)
        if amount < _MIN_STAKE:
            amount = min(staked, _MIN_STAKE)
        if self._earn_order("Redeem", amount):
            logger.info("[Крипта] Earn: вернул %.2f$ под заявку", amount)
            time.sleep(1.0)

    def _earn_park_idle(self) -> None:
        """Свободный кэш сверх буфера — под проценты."""
        if not self._earn_enabled():
            return
        try:
            free, _positions = self._wallet(with_earn=False)
        except Exception:
            return
        amount = free - _FREE_BUFFER
        if amount < _MIN_STAKE:
            return
        if self._earn_order("Stake", amount):
            logger.info("[Крипта] Earn: разместил %.2f$ под проценты", amount)
