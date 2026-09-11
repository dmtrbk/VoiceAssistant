# skills/groq_client.py
# Один клиент Groq на процесс. Цепочка моделей общая для чата и биржи.

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Iterator

_LOCK = threading.Lock()
_client = None

# qwen/qwen3.8-27b — несуществующий id (таймаут на каждый fallback).
# llama-3.3-70b-versatile — снят с free/developer 16.08.2026.
FALLBACK_MODELS = (
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-120b",
)

_MODEL_MISS = ("model", "not found", "unknown", "404", "400")


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
    return any(marker in err for marker in _MODEL_MISS + extra)


def get_client():
    """Синхронный Groq. max_retries=0 — иначе ~0.4 с Retrying до каждой реплики."""
    global _client
    with _LOCK:
        if _client is not None:
            return _client
        key = (os.getenv("GROQ_API_KEY") or "").strip().strip("\"'")
        if not key:
            return None
        from groq import Groq

        _client = Groq(api_key=key, max_retries=0, timeout=8.0)
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
    if "gpt-oss" in (model_name or ""):
        kwargs["reasoning_effort"] = "low"
    return kwargs


def complete_one(
    messages: list,
    model_name: str,
    temperature: float,
    max_tokens: int,
) -> str:
    client = get_client()
    if client is None:
        raise RuntimeError("Groq client unavailable")
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
                logging.warning("[Groq] Модель %s недоступна, пробую fallback", model_name)
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("No response from Groq")


def stream_tokens(
    messages: list,
    model_name: str,
    temperature: float,
    max_tokens: int,
    abort: Callable[[], bool] | None = None,
) -> Iterator[str]:
    client = get_client()
    if client is None:
        raise RuntimeError("Groq client unavailable")
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
