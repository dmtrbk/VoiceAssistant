# tts_cache.py

import os
import hashlib
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".tts_cache")

PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL_NAME = os.getenv("PIPER_MODEL", "ru_RU-dmitri-medium.onnx")
PIPER_MODEL = os.path.join(PIPER_DIR, "models", PIPER_MODEL_NAME)
VOICE_SPEED = os.getenv("VOICE_SPEED", "1.0")
VOICE_SPEAKER = os.getenv("VOICE_SPEAKER", None)

# Максимальная длина фразы для постоянного кэширования (длинные уникальные ответы Groq не засоряют диск)
MAX_CACHE_TEXT_LEN = 250


def init_cache_dir() -> str:
    """Создает директорию кэша, если её нет."""
    if not os.path.exists(CACHE_DIR):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except Exception as e:
            logger.error(f"[TTS Cache] Не удалось создать директорию кэша {CACHE_DIR}: {e}")
    return CACHE_DIR


def get_cache_key(text: str, model_path: str = PIPER_MODEL, speed: str = VOICE_SPEED, speaker: str | None = VOICE_SPEAKER) -> str:
    """Формирует уникальный MD5 хэш для текста и параметров синтезатора."""
    norm_text = text.strip()
    model_key = os.path.basename(model_path)
    raw = f"{model_key}_{speed}_{speaker or 'default'}_{norm_text}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def get_cached_audio_path(text: str) -> str | None:
    """Возвращает путь к сохраненному wav-файлу, если он уже есть в кэше."""
    if len(text.strip()) > MAX_CACHE_TEXT_LEN:
        return None
    
    init_cache_dir()
    key = get_cache_key(text)
    file_path = os.path.join(CACHE_DIR, f"{key}.wav")
    if os.path.exists(file_path) and os.path.getsize(file_path) > 44:  # 44 байта — мин. заголовок WAV
        return file_path
    return None


def synthesize_to_file(text: str, output_path: str) -> bool:
    """Синтезирует аудио через Piper напрямую в указанный файл."""
    if not text or not os.path.exists(PIPER_EXE) or not os.path.exists(PIPER_MODEL):
        return False

    cmd = [
        PIPER_EXE,
        "--model", PIPER_MODEL,
        "--output_file", output_path,
        "--length_scale", VOICE_SPEED
    ]
    if VOICE_SPEAKER is not None:
        cmd.extend(["--speaker", VOICE_SPEAKER])

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8"
        )
        _, stderr = proc.communicate(input=text)
        if proc.returncode != 0:
            logger.error(f"[TTS] Ошибка Piper: {stderr.strip() if stderr else 'неизвестная ошибка'}")
            return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 44
    except Exception as e:
        logger.error(f"[TTS] Исключение при синтезе: {e}")
        return False


def get_or_synthesize_wav(text: str, temp_fallback_path: str = "/dev/shm/tts_output.wav") -> tuple[str, bool]:
    """
    Получает путь к WAV-файлу.
    Возвращает (path, was_cached: bool).
    """
    clean_text = text.strip()
    if not clean_text:
        return "", False

    # 1. Проверяем кэш
    cached_path = get_cached_audio_path(clean_text)
    if cached_path:
        return cached_path, True

    # 2. Если фраза короткая / постоянная — сохраняем прямо в кэш
    if len(clean_text) <= MAX_CACHE_TEXT_LEN:
        init_cache_dir()
        key = get_cache_key(clean_text)
        target_path = os.path.join(CACHE_DIR, f"{key}.wav")
        if synthesize_to_file(clean_text, target_path):
            return target_path, False

    # 3. Для длинных уникальных реплик используем fallback (RAM-диск)
    target_path = temp_fallback_path if os.path.exists("/dev/shm") else os.path.join(BASE_DIR, "tts_output.wav")
    success = synthesize_to_file(clean_text, target_path)
    return (target_path if success else ""), False


def precache_common_phrases(phrases: list[str]) -> int:
    """Предварительно генерирует аудио для списка частых фраз при старте."""
    cached_count = 0
    init_cache_dir()
    for phrase in phrases:
        clean = phrase.strip()
        if not clean:
            continue
        key = get_cache_key(clean)
        target = os.path.join(CACHE_DIR, f"{key}.wav")
        if not os.path.exists(target) or os.path.getsize(target) <= 44:
            if synthesize_to_file(clean, target):
                cached_count += 1
    return cached_count
