# stt.py
# Локальное распознавание речи через Vosk.
# PhraseAudioBuffer оставлен для захвата фразы в assistant.py.

from __future__ import annotations

import threading


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


def transcribe_audio(
    pcm_bytes: bytes,
    fallback_text: str = "",
    sample_rate: int = 16000,
) -> str:
    """Возвращает текст Vosk. Облачный Whisper отключён — только оффлайн."""
    _ = pcm_bytes, sample_rate
    return fallback_text
