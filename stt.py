# stt.py
# Онлайн / гибридное распознавание речи (Groq Whisper Turbo + Vosk)
# Локальный Vosk: wake-words, barge-in стоп-слова, аварийные стопы, оффлайн fallback.
# Groq Whisper: высокоточное распознавание реплик диалога, слотов и сложных команд.

from __future__ import annotations

import io
import logging
import os
import re
import threading
import wave
from typing import Optional

# Паттерны, при наличии которых фраза целиком признается галлюцинацией тишины
_FULL_HALLUCINATION_PATTERNS = (
    r"продолжение следует",
    r"субтитр",
    r"дима торжок",
    r"dima torzok",
    r"спасибо за просмотр",
    r"ставьте лайк",
    r"подписывай",
)
_FULL_HALLUCINATION_RE = re.compile("|".join(_FULL_HALLUCINATION_PATTERNS), flags=re.IGNORECASE)

# Паттерны для вырезания шумов в скобках [музыка], (смех)
_INLINE_NOISE_RE = re.compile(r"\[.*?\]|\(.*?\)")


def clean_whisper_text(text: str) -> str:
    """Очищает результат транскрипции Whisper от шума и галлюцинаций тишины."""
    if not text:
        return ""
    cleaned = text.strip().strip("\"'«»")
    if _FULL_HALLUCINATION_RE.search(cleaned):
        return ""
    if _INLINE_NOISE_RE.search(cleaned):
        cleaned = _INLINE_NOISE_RE.sub(" ", cleaned).strip()
    # Если остались только знаки препинания или пустота
    if not re.search(r"[\wа-яА-ЯёЁ]", cleaned):
        return ""
    return re.sub(r"\s+", " ", cleaned).strip()


def pcm_to_wav(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
) -> bytes:
    """Упаковывает сырые 16-битные PCM-сэмплы в формат WAV в памяти."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class PhraseAudioBuffer:
    """
    Кольцевой буфер звука:
    - В покое (нет речи) удерживает последние pre_roll_sec секунд аудио-фона,
      чтобы не обрезать первый слог при активации.
    - При фиксации речи переходит в режим полного накопления.
    - При завершении фразы отдает накопленные байты PCM и сбрасывается.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        pre_roll_sec: float = 1.5,
        max_phrase_sec: float = 15.0,
    ):
        self.sample_rate = sample_rate
        self.bytes_per_sec = sample_rate * 2  # 16-bit mono
        self.pre_roll_bytes = int(pre_roll_sec * self.bytes_per_sec)
        self.max_bytes = int(max_phrase_sec * self.bytes_per_sec)
        self._buffer = bytearray()
        self._has_speech = False
        self._lock = threading.Lock()

    def add_chunk(self, chunk: bytes, has_speech: bool = False) -> None:
        with self._lock:
            self._buffer.extend(chunk)
            if has_speech:
                self._has_speech = True
                if len(self._buffer) > self.max_bytes:
                    self._buffer = self._buffer[-self.max_bytes:]
            elif not self._has_speech:
                if len(self._buffer) > self.pre_roll_bytes:
                    self._buffer = self._buffer[-self.pre_roll_bytes:]

    def get_and_reset(self) -> bytes:
        with self._lock:
            data = bytes(self._buffer)
            self._buffer.clear()
            self._has_speech = False
            return data

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._has_speech = False


def transcribe_groq_whisper(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    model: str = "whisper-large-v3-turbo",
    language: str = "ru",
    timeout: float = 4.0,
) -> Optional[str]:
    """
    Отправляет аудио в облачный Groq Whisper API.
    Возвращает очищенную строку или None при сбое сети/ошибке.
    """
    # Минимум ~0.35 секунды звука для транскрипции
    min_bytes = int(sample_rate * 2 * 0.35)
    if not pcm_bytes or len(pcm_bytes) < min_bytes:
        return None

    try:
        from skills.groq_client import get_client

        client = get_client()
        if client is None:
            return None

        wav_data = pcm_to_wav(pcm_bytes, sample_rate=sample_rate)
        response = client.audio.transcriptions.create(
            model=model,
            file=("speech.wav", io.BytesIO(wav_data), "audio/wav"),
            language=language,
            response_format="text",
        )
        raw_text = response if isinstance(response, str) else getattr(response, "text", str(response))
        cleaned = clean_whisper_text(raw_text)
        return cleaned or None
    except Exception as exc:
        logging.warning("[STT Groq] Ошибка транскрипции Whisper: %s", exc)
        return None


def transcribe_audio(
    pcm_bytes: bytes,
    fallback_text: str = "",
    sample_rate: int = 16000,
) -> str:
    """
    Распознает аудио:
    1. Если включен гибридный режим и доступен Groq Whisper — пробует онлайн.
    2. При успехе возвращает онлайн-текст.
    3. При сбое сети или отключенном режиме — возвращает fallback_text (Vosk).
    """
    try:
        from skill_settings import is_online_stt_enabled

        if is_online_stt_enabled():
            online_result = transcribe_groq_whisper(pcm_bytes, sample_rate=sample_rate)
            if online_result:
                logging.info("[STT Groq Whisper] '%s' (Vosk: '%s')", online_result, fallback_text)
                return online_result
            if fallback_text:
                logging.info("[STT Fallback] Whisper не дал результат, используем Vosk: '%s'", fallback_text)
    except Exception as exc:
        logging.warning("[STT] Сбой онлайн распознавания: %s", exc)

    return fallback_text
