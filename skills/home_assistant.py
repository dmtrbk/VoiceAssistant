# skills/home_assistant.py
# Голос Джарвиса на ноуте → REST API Home Assistant (HAOS в Boxes / LAN).

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import requests

from skills.ai_chat import log_system_action
from skills.base import BaseSkill, RequestContext
from browser import is_close_browser_text
from skills.text_utils import fuzzy_phrase_match, norm as _norm
from window_control import is_window_command_text

logger = logging.getLogger(__name__)

_CACHE_SEC = 45.0
_CONTROLLABLE = {
    "light", "switch", "fan", "input_boolean", "cover", "lock",
    "scene", "script", "automation", "media_player", "climate",
}

_HA_MARKERS = (
    "умный дом", "умного дома", "умном доме",
    "home assistant", "хоум ассистент", "хоум ассистант",
    "домашний ассистент",
)
_LIST_HINTS = (
    "какие устройства", "что включено", "что горит",
    "список устройств", "состояние дома", "что открыто",
)
_ON = ("включи", "зажги", "открой", "активируй", "вруби")
_OFF = ("выключи", "потуши", "закрой", "выруби", "погаси")


def _strip_fillers(text: str) -> str:
    text = _norm(text)
    for marker in _HA_MARKERS:
        text = text.replace(marker, " ")
    text = re.sub(
        r"\b(пожалуйста|джарвис|умник|гаврила|гаврюша|гаврик|гаврило|в доме|дома)\b",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


class HomeAssistantSkill(BaseSkill):
    """Включение, выключение и сводка устройств Home Assistant по имени."""

    def __init__(self) -> None:
        self._reload_env()
        self._states: list[dict[str, Any]] = []
        self._states_at = 0.0
        self._last_entity: dict[str, Any] | None = None

    def _reload_env(self) -> None:
        self._url = (os.getenv("HA_URL") or "http://127.0.0.1:8123").strip().rstrip("/")
        self._token = (os.getenv("HA_TOKEN") or os.getenv("HOMEASSISTANT_TOKEN") or "").strip()

    def on_disabled(self) -> None:
        self._states = []
        self._last_entity = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _refresh_states(self, force: bool = False) -> bool:
        if not self._token:
            return False
        if not force and self._states_at and (time.time() - self._states_at) < _CACHE_SEC:
            return bool(self._states)
        try:
            resp = requests.get(
                f"{self._url}/api/states",
                headers=self._headers(),
                timeout=5,
            )
            if resp.status_code == 401:
                logger.warning("[HA] Токен отклонён.")
                self._states = []
                self._states_at = time.time()
                return False
            resp.raise_for_status()
            raw = resp.json()
            if not isinstance(raw, list):
                self._states = []
                self._states_at = time.time()
                return False
            useful: list[dict[str, Any]] = []
            for item in raw:
                entity_id = str(item.get("entity_id") or "")
                domain = entity_id.split(".", 1)[0]
                if domain not in _CONTROLLABLE:
                    continue
                attrs = item.get("attributes") or {}
                name = str(attrs.get("friendly_name") or entity_id.split(".", 1)[-1])
                useful.append({
                    "entity_id": entity_id,
                    "domain": domain,
                    "name": name,
                    "name_norm": _norm(name),
                    "state": str(item.get("state") or ""),
                })
            self._states = useful
            self._states_at = time.time()
            return True
        except Exception as exc:
            logger.warning("[HA] Не удалось получить состояния: %s", exc)
            self._states = []
            self._states_at = time.time()
            return False

    def _match_entity(self, text: str) -> dict[str, Any] | None:
        hay = _strip_fillers(text)
        for verb in _ON + _OFF:
            hay = hay.replace(verb, " ")
        hay = re.sub(r"\b(сцену|сцена|сценарий)\b", " ", hay)
        hay = re.sub(r"\s+", " ", hay).strip()
        if len(hay) < 3:
            return None
        best: dict[str, Any] | None = None
        best_len = 0
        for ent in self._states:
            candidates = {ent["name_norm"]}
            object_id = ent["entity_id"].split(".", 1)[-1].replace("_", " ")
            candidates.add(_norm(object_id))
            for cand in candidates:
                if len(cand) < 3:
                    continue
                hit = (
                    cand in hay
                    or hay in cand
                    or fuzzy_phrase_match(hay, cand, min_ratio=0.72)
                )
                if hit and len(cand) > best_len:
                    best = ent
                    best_len = len(cand)
        return best

    def can_handle(self, context: RequestContext) -> bool:
        text = _norm(context.raw_text)
        if not text:
            return False
        if any(marker in text for marker in _HA_MARKERS):
            return True
        if any(hint in text for hint in _LIST_HINTS):
            return True
        if re.search(r"\bсцен(а|у|ы|ой)\b", text) and (
            any(verb in text for verb in _ON + _OFF) or any(marker in text for marker in _HA_MARKERS)
        ):
            return True
        if any(verb in text for verb in _ON + _OFF):
            if is_window_command_text(text) or is_close_browser_text(text):
                return False
            if not self._states:
                self._refresh_states()
            return bool(self._states) and self._match_entity(text) is not None
        return False

    def accepts_followup(self, context: RequestContext) -> bool:
        text = _norm(context.raw_text)
        words = re.findall(r"[а-яa-z0-9\-]+", text)
        if not words or len(words) > 8:
            return False
        if any(hint in text for hint in _LIST_HINTS) or re.search(r"\bсцен(а|у|ы|ой)\b", text):
            return True
        if any(verb in text for verb in _ON + _OFF):
            return True
        return bool(self._states) and self._match_entity(text) is not None

    def _call_service(self, domain: str, service: str, entity_id: str) -> bool:
        try:
            resp = requests.post(
                f"{self._url}/api/services/{domain}/{service}",
                headers=self._headers(),
                json={"entity_id": entity_id},
                timeout=8,
            )
            if resp.status_code in (200, 201):
                return True
            logger.warning("[HA] %s/%s %s → %s", domain, service, entity_id, resp.status_code)
            return False
        except Exception as exc:
            logger.warning("[HA] Сервис %s/%s: %s", domain, service, exc)
            return False

    def _turn(self, entity: dict[str, Any], turn_on: bool) -> bool:
        domain = entity["domain"]
        entity_id = entity["entity_id"]
        if domain == "cover":
            service = "open_cover" if turn_on else "close_cover"
        elif domain == "lock":
            service = "unlock" if turn_on else "lock"
        else:
            service = "turn_on" if turn_on else "turn_off"
        ok = self._call_service(domain, service, entity_id)
        if ok:
            self._last_entity = entity
            log_system_action(
                f"{'Включил' if turn_on else 'Выключил'} {entity['name']} в Home Assistant"
            )
        return ok

    def _speak_list(self, context: RequestContext) -> None:
        active = [
            ent for ent in self._states
            if ent["state"] in {"on", "open", "unlocked", "playing", "home"}
            and ent["domain"] != "scene"
        ]
        if not active:
            context.speak("В умном доме сейчас ничего явно не включено.")
            return
        names = [ent["name"] for ent in active[:6]]
        tail = "" if len(active) <= 6 else f" И ещё {len(active) - 6}."
        context.speak("Сейчас включено: " + ", ".join(names) + "." + tail)

    def execute(self, context: RequestContext) -> None:
        self._reload_env()
        text = _norm(context.raw_text)
        if not self._token:
            context.speak(
                "Для умного дома нужен токен Home Assistant. "
                "Пропиши HA_TOKEN в файле энв."
            )
            return

        if not self._refresh_states(force=True):
            context.speak("Дом не ответил. Проверь, что Home Assistant запущен.")
            return

        if any(hint in text for hint in _LIST_HINTS) and not any(v in text for v in _ON + _OFF):
            self._speak_list(context)
            return

        want_on = any(v in text for v in _ON)
        want_off = any(v in text for v in _OFF)
        entity = self._match_entity(text)
        if entity is None and (want_on or want_off) and self._last_entity:
            entity = self._last_entity

        if entity is None:
            if any(marker in text for marker in _HA_MARKERS):
                self._speak_list(context)
                return
            context.speak("Не нашёл такое устройство в умном доме.")
            return

        if want_off:
            ok = self._turn(entity, False)
            context.speak(f"Выключил {entity['name']}." if ok else f"Не смог выключить {entity['name']}.")
            return
        if want_on or entity["domain"] in {"scene", "script"}:
            ok = self._turn(entity, True)
            context.speak(f"Включил {entity['name']}." if ok else f"Не смог включить {entity['name']}.")
            return

        state = entity["state"]
        spoken = {"on": "включено", "off": "выключено", "open": "открыто", "closed": "закрыто"}.get(state, state)
        context.speak(f"{entity['name']} сейчас {spoken}.")
