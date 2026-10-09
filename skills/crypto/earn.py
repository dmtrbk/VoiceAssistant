# skills/crypto/earn.py
# Простаивающий кэш под проценты Bybit Flexible Saving (USDT).
# Вывод мгновенный, поэтому держим там всё свободное, а перед покупкой возвращаем нужное.

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import common

logger = logging.getLogger(__name__)

_CATEGORY = "FlexibleSaving"
_PRODUCT_ID = "1"  # USDT Flexible Saving
_MIN_STAKE = 1.5  # минимум продукта
# Свободные доллары на споте — для ручной покупки. Остальное под проценты.
_FREE_BUFFER = 10.0
_POSITION_CACHE_SEC = 20.0


def _earn_tx_id(row: dict[str, Any]) -> str:
    tid = str(row.get("id") or row.get("transactionId") or "")
    if tid:
        return tid
    return "|".join(
        (
            str(row.get("type") or ""),
            str(row.get("transactionTime") or ""),
            str(row.get("cashFlow") or ""),
        )
    )


def _read_earn_state() -> dict[str, Any]:
    path = common._EARN_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_earn_state(payload: dict[str, Any]) -> None:
    path = common._EARN_PATH
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("[Крипта] Earn: не записал учёт процентов: %s", exc)


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

    def _earn_interest(self) -> float:
        """Накопленный процент: позиция плюс все размещения и возвраты за всё время.

        Считаем по своей копии истории. Последние 50 операций биржи со временем
        теряют саму заявку «положил», и цифра прыгнула бы на размер вклада.
        """
        if not self._earn_enabled():
            return 0.0
        flow = self._earn_flow()
        if flow is None:
            return 0.0
        return max(0.0, flow + self._earn_staked(fresh=True))

    def _earn_flow(self) -> float | None:
        state = _read_earn_state()
        seen = set(state.get("seen") or [])
        flow = float(state.get("flow") or 0)
        cursor = ""
        changed = False
        try:
            for _page in range(40):
                params = {"accountType": "UNIFIED", "currency": "USDT", "limit": "50"}
                if cursor:
                    params["cursor"] = cursor
                data = self._signed("GET", "/v5/account/transaction-log", params)
                result = data.get("result") or {}
                rows = result.get("list") or []
                fresh = False
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    tid = _earn_tx_id(row)
                    if tid in seen:
                        continue
                    seen.add(tid)
                    fresh = True
                    changed = True
                    if str(row.get("type") or "").startswith("FLEXIBLE_STAKING"):
                        flow += float(row.get("cashFlow") or 0)
                cursor = str(result.get("nextPageCursor") or "")
                if not rows or not cursor or not fresh:
                    break
        except Exception as exc:
            logger.info("[Крипта] Earn: история операций недоступна (%s)", exc)
            if not state:
                return None
        if changed:
            _write_earn_state({"flow": round(flow, 8), "seen": sorted(seen)})
        return flow

    def _earn_park_idle(self) -> None:
        """Свободный кэш сверх буфера — под проценты."""
        if not self._earn_enabled():
            return
        try:
            free, _positions = self._wallet(with_earn=False)
        except Exception:
            return
        if free + 1e-9 < _FREE_BUFFER:
            gap = _FREE_BUFFER - free
            staked = self._earn_staked(fresh=True)
            if staked <= 0:
                return
            amount = min(staked, gap)
            if amount < _MIN_STAKE:
                amount = min(staked, _MIN_STAKE)
            if amount < _MIN_STAKE:
                return
            if self._earn_order("Redeem", amount):
                logger.info(
                    "[Крипта] Earn: вернул %.2f$ — на споте %.0f$ для ручной покупки",
                    amount,
                    _FREE_BUFFER,
                )
            return
        amount = free - _FREE_BUFFER
        if amount < _MIN_STAKE:
            return
        if self._earn_order("Stake", amount):
            logger.info("[Крипта] Earn: разместил %.2f$ под проценты", amount)
