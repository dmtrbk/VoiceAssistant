# skills/openai_client.py
# Один OpenAI-совместимый клиент на процесс (по умолчанию OpenRouter).
# Цепочка моделей общая для чата и биржи.

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Iterator

_LOCK = threading.Lock()
_client = None

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"

# id в формате OpenRouter (provider/model).
FAST_MODEL = "qwen/qwen3.8-27b:free"
STRONG_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
FALLBACK_MODELS = (
    FAST_MODEL,
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    STRONG_MODEL,
)

# id, подпись в окне настроек. Старт — быстрая; сильная — когда Cursor закрыт.
OPENAI_MODEL_CHOICES = (
    (FAST_MODEL, "Быстрая — Qwen3.8 27B free"),
    (STRONG_MODEL, "Сильная — Nemotron Super 120B free"),
)

_MODEL_MISS = ("model", "not found", "unknown", "404", "400")
_RETRY_TRANSIENT = ("timeout", "timed out", "temporarily", "429", "rate limit", "overloaded")


def normalize_openai_model(raw: str | None) -> str:
    clean = (raw or "").strip()
    return clean or FAST_MODEL


def openai_model_choices(current: str | None = None) -> list[tuple[str, str]]:
    try:
        from skills.model_catalog import settings_model_choices

        return settings_model_choices(current)
    except Exception:
        rows = list(OPENAI_MODEL_CHOICES)
        seen = {item[0] for item in rows}
        extra = (current or "").strip()
        if extra and extra not in seen:
            rows.append((extra, extra))
        return rows


def model_chain(preferred: str | None = None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for name in (preferred, *FALLBACK_MODELS):
        clean = (name or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            ordered.append(clean)
    return ordered


def is_retriable_model_error(exc: BaseException, extra: tuple[str, ...] = ()) -> bool:
    err = str(exc).lower()
    return any(marker in err for marker in _MODEL_MISS + _RETRY_TRANSIENT + extra)


def _api_key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip().strip("\"'")


def _api_base() -> str:
    raw = (os.getenv("OPENAI_API_BASE") or "").strip().strip("\"'")
    return raw or DEFAULT_API_BASE


def get_client():
    """Синхронный OpenAI SDK. max_retries=0 — без лишней задержки Retrying."""
    global _client
    with _LOCK:
        if _client is not None:
            return _client
        key = _api_key()
        if not key:
            return None
        from openai import OpenAI

        try:
            import httpx

            timeout: Any = httpx.Timeout(connect=8.0, read=45.0, write=10.0, pool=5.0)
        except Exception:
            timeout = 45.0
        _client = OpenAI(
            api_key=key,
            base_url=_api_base(),
            max_retries=0,
            timeout=timeout,
            default_headers={
                "HTTP-Referer": "https://github.com/local/VoiceAssistant",
                "X-Title": "VoiceAssistant Jarvis",
            },
        )
        return _client


def chat_kwargs(
    messages: list,
    model_name: str,
    temperature: float,
    max_tokens: int,
    stream: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "messages": messages,
        "model": model_name,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        kwargs["stream"] = True
    return kwargs


def complete_one(
    messages: list,
    model_name: str,
    temperature: float,
    max_tokens: int,
) -> str:
    client = get_client()
    if client is None:
        raise RuntimeError("OpenAI client unavailable")
    response = client.chat.completions.create(
        **chat_kwargs(messages, model_name, temperature, max_tokens)
    )
    return response.choices[0].message.content or ""


def complete(
    messages: list,
    preferred: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
) -> str:
    last_err: Exception | None = None
    for model_name in model_chain(preferred):
        try:
            return complete_one(messages, model_name, temperature, max_tokens)
        except Exception as exc:
            last_err = exc
            if is_retriable_model_error(exc):
                logging.warning("[LLM] Модель %s недоступна, пробую fallback", model_name)
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("No response from OpenAI-compatible API")


def stream_tokens(
    messages: list,
    model_name: str,
    temperature: float,
    max_tokens: int,
    abort: Callable[[], bool] | None = None,
) -> Iterator[str]:
    client = get_client()
    if client is None:
        raise RuntimeError("OpenAI client unavailable")
    if abort and abort():
        return
    stream = client.chat.completions.create(
        **chat_kwargs(messages, model_name, temperature, max_tokens, stream=True)
    )
    try:
        for chunk in stream:
            if abort and abort():
                return
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None) or ""
            if piece:
                yield piece
    finally:
        closer = getattr(stream, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
