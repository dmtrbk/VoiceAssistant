# cursor_bridge.py
# Один локальный агент Cursor на все сообщения Telegram.
# Идентификатор чата лежит рядом, в git не попадает.

from __future__ import annotations

import json
import logging
import os

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_ID_PATH = os.path.join(_PROJECT_DIR, "cursor_telegram_agent.json")
_MODEL = (os.getenv("CURSOR_MODEL") or "composer-2.5").strip() or "composer-2.5"
_MAX_CHARS = 4000

_FIRST = (
    "Ты отвечаешь хозяину в Telegram. Пиши по-русски, коротко, как сообщение в чат. "
    "Репозиторий — этот проект. Если просят изменить код — меняй. "
    "Если это вопрос или разговор — ответь текстом и не трогай файлы.\n\n"
)


def _api_key() -> str:
    return (os.getenv("CURSOR_API_KEY") or "").strip().strip("\"'")


def _load_agent_id() -> str:
    try:
        with open(_ID_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(raw, dict):
        return str(raw.get("agent_id") or "").strip()
    return ""


def _save_agent_id(agent_id: str) -> None:
    temp = _ID_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"agent_id": agent_id}, handle)
    os.replace(temp, _ID_PATH)


def _forget_agent_id() -> None:
    try:
        os.remove(_ID_PATH)
    except OSError:
        pass


def _options(api_key: str):
    from cursor_sdk import AgentOptions, LocalAgentOptions

    return AgentOptions(
        api_key=api_key,
        model=_MODEL,
        local=LocalAgentOptions(cwd=_PROJECT_DIR),
    )


def _agent_id_of(agent) -> str:
    return str(getattr(agent, "agent_id", None) or getattr(agent, "agentId", None) or "").strip()


def _send(agent, prompt: str) -> str:
    run = agent.send(prompt)
    logging.info(
        "[Cursor] agent=%s run=%s",
        _agent_id_of(agent),
        getattr(run, "id", ""),
    )
    result = run.wait()
    status = str(getattr(result, "status", "") or "")
    if status == "error":
        raise RuntimeError("run status=error")
    return str(getattr(result, "result", "") or "").strip()


def _once(prompt: str, api_key: str, agent_id: str) -> str:
    from cursor_sdk import Agent

    options = _options(api_key)
    factory = Agent.resume(agent_id, options) if agent_id else Agent.create(options)
    with factory as agent:
        saved = _agent_id_of(agent)
        if saved:
            _save_agent_id(saved)
        return _send(agent, prompt)


def ask_cursor(text: str) -> str:
    """Один ход в постоянном локальном чате. Пустой ключ — короткая ошибка, без исключения."""
    key = _api_key()
    if not key:
        return "В .env нет CURSOR_API_KEY."
    clean = (text or "").strip()
    if not clean:
        return "Пустое сообщение."

    from cursor_sdk import CursorAgentError

    agent_id = _load_agent_id()
    prompt = (_FIRST + clean) if not agent_id else clean
    try:
        reply = _once(prompt, key, agent_id)
    except CursorAgentError as exc:
        logging.warning("[Cursor] Не стартовал (%s), пробую новый чат.", exc)
        if not agent_id:
            return "Cursor не ответил."
        _forget_agent_id()
        try:
            reply = _once(_FIRST + clean, key, "")
        except CursorAgentError:
            return "Cursor не ответил."
        except Exception as exc:
            logging.error("[Cursor] Ошибка нового чата: %s", exc)
            return "Cursor не ответил."
    except Exception as exc:
        logging.error("[Cursor] Ошибка ответа: %s", exc)
        return "Cursor не ответил."
    return reply or "Пустой ответ."


def telegram_chunks(text: str, limit: int = _MAX_CHARS) -> list[str]:
    body = (text or "").strip() or "Пустой ответ."
    if len(body) <= limit:
        return [body]
    parts: list[str] = []
    rest = body
    while rest:
        parts.append(rest[:limit])
        rest = rest[limit:]
    return parts
