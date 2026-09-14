# skills/groq_client.py
# Один клиент Groq на процесс. Цепочка моделей общая для чата и биржи.
# Каталог Groq — чат, речь и зрение (понимание фото). Рисовать картинки API не умеет.

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Iterator

_LOCK = threading.Lock()
_client = None

# qwen/qwen3.8-27b ловил таймаут на fallback; llama-3.3-70b снят с free 16.08.2026.
FAST_MODEL = "openai/gpt-oss-20b"
STRONG_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODELS = (
    FAST_MODEL,
    "qwen/qwen3.6-27b",
    STRONG_MODEL,
)

# id, подпись в окне настроек. Старт — быстрая; сильная — когда Cursor закрыт.
GROQ_MODEL_CHOICES = (
    (FAST_MODEL, "Быстрая — GPT-OSS 20B"),
    (STRONG_MODEL, "Сильная — GPT-OSS 120B"),
)


def normalize_groq_model(raw: str | None) -> str:
    clean = (raw or "").strip()
    return clean or FAST_MODEL


def groq_model_choices(current: str | None = None) -> list[tuple[str, str]]:
    rows = list(GROQ_MODEL_CHOICES)
    seen = {item[0] for item in rows}
    extra = (current or "").strip()
    if extra and extra not in seen:
        rows.append((extra, extra))
    return rows


# Снимок публичного каталога console.groq.com/docs/models (сентябрь 2026).
# Нужен тестам list_models: в списке нет генерации картинок.
GROQ_CATALOG_IDS = (
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "whisper-large-v3",
    "whisper-large-v3-turbo",
    "groq/compound",
    "groq/compound-mini",
    "canopylabs/orpheus-arabic-saudi",
    "canopylabs/orpheus-v1-english",
    "meta-llama/llama-prompt-guard-2-22m",
    "meta-llama/llama-prompt-guard-2-86m",
    "minimaxai/minimax-m2.7",
    "openai/gpt-oss-safeguard-20b",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.8-27b",
)

CAP_CHAT = "chat"
CAP_SPEECH = "speech"
CAP_TTS = "tts"
CAP_VISION = "vision"
CAP_IMAGE_GEN = "image_gen"
CAP_GUARD = "guard"

_CAP_LABELS = {
    CAP_CHAT: "чат",
    CAP_SPEECH: "речь → текст",
    CAP_TTS: "текст → речь",
    CAP_VISION: "зрение (понимает картинку, не рисует)",
    CAP_IMAGE_GEN: "генерация картинок",
    CAP_GUARD: "фильтр",
}
_CAP_ORDER = (CAP_CHAT, CAP_SPEECH, CAP_TTS, CAP_VISION, CAP_GUARD, CAP_IMAGE_GEN)
_VISION_MARKERS = (
    "qwen3.6",
    "qwen3.8",
    "llama-4-scout",
    "llama-4-maverick",
    "llava",
    "vision",
)
_IMAGE_GEN_MARKERS = (
    "flux",
    "stable-diffusion",
    "dall-e",
    "dalle",
    "imagen",
    "sdxl",
    "image-gen",
    "image_gen",
)


def groq_model_caps(model_id: str) -> frozenset[str]:
    """Возможности модели по id. Groq рисует только если id явно генеративный."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return frozenset()
    caps: set[str] = set()
    if "whisper" in mid:
        caps.add(CAP_SPEECH)
    elif "orpheus" in mid:
        caps.add(CAP_TTS)
    elif "guard" in mid or "safeguard" in mid:
        caps.add(CAP_GUARD)
    else:
        caps.add(CAP_CHAT)
    if any(marker in mid for marker in _VISION_MARKERS):
        caps.add(CAP_VISION)
        caps.add(CAP_CHAT)
    if any(marker in mid for marker in _IMAGE_GEN_MARKERS):
        caps.add(CAP_IMAGE_GEN)
    return frozenset(caps)


def groq_draws_images(model_ids: list[str] | tuple[str, ...] | None = None) -> bool:
    ids = GROQ_CATALOG_IDS if model_ids is None else model_ids
    return any(CAP_IMAGE_GEN in groq_model_caps(mid) for mid in ids)


def format_groq_models_report(model_ids: list[str] | tuple[str, ...]) -> str:
    lines = ["=== Доступные модели Groq ==="]
    for mid in model_ids:
        caps = groq_model_caps(mid)
        labels = ", ".join(_CAP_LABELS[cap] for cap in _CAP_ORDER if cap in caps) or "неизвестно"
        lines.append(f"- {mid}  [{labels}]")
    lines.append("")
    if groq_draws_images(model_ids):
        lines.append("Картинки: в каталоге есть модель генерации — можно рисовать через Groq.")
    else:
        lines.append(
            "Картинки: в каталоге Groq нет моделей рисования. "
            "Зрение (Qwen) только описывает фото. "
            "Навык «нарисуй» идёт в Pollinations; Groq лишь разворачивает промпт."
        )
    return "\n".join(lines)


_MODEL_MISS = ("model", "not found", "unknown", "404", "400")
_RETRY_TRANSIENT = ("timeout", "timed out", "temporarily", "429", "rate limit", "overloaded")


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

        try:
            import httpx

            timeout: Any = httpx.Timeout(connect=8.0, read=45.0, write=10.0, pool=5.0)
        except Exception:
            timeout = 45.0
        _client = Groq(api_key=key, max_retries=0, timeout=timeout)
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
