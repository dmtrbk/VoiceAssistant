# tts_cache.py

import os
import hashlib
import logging
import subprocess

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".tts_cache")

PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL_NAME = os.getenv("PIPER_MODEL", "ru_RU-dmitri-medium.onnx")
PIPER_MODEL = os.path.join(PIPER_DIR, "models", PIPER_MODEL_NAME)
VOICE_SPEED = os.getenv("VOICE_SPEED", "1.0")
VOICE_SPEAKER = os.getenv("VOICE_SPEAKER", None)

SYSTEM_CACHE_PHRASES = [
    "Да?",
    "Слушаю вас",
    "Я здесь",
    "Тут я",
    "На связи!",
    "Да-да, слушаю",
    "Готов к работе",
    "Этот навык сейчас выключен.",
    "Не расслышал, повторите, пожалуйста.",
    "До связи!",
]


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
    init_cache_dir()
    key = get_cache_key(text)
    file_path = os.path.join(CACHE_DIR, f"{key}.wav")
    if os.path.exists(file_path) and os.path.getsize(file_path) > 44:
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


def get_or_synthesize_wav(
    text: str,
    temp_fallback_path: str = "/dev/shm/tts_output.wav",
    persist: bool = False,
) -> tuple[str, bool]:
    """
    Синтезирует WAV. На диск в .tts_cache пишет только persist=True
    (прогрев системных фраз). Остальное — во временный файл.
    """
    clean_text = text.strip()
    if not clean_text:
        return "", False

    cached_path = get_cached_audio_path(clean_text)
    if cached_path:
        return cached_path, True

    if persist:
        init_cache_dir()
        key = get_cache_key(clean_text)
        target_path = os.path.join(CACHE_DIR, f"{key}.wav")
        if synthesize_to_file(clean_text, target_path):
            return target_path, False

    target_path = temp_fallback_path
    if target_path.startswith("/dev/shm") and not os.path.exists("/dev/shm"):
        target_path = os.path.join(BASE_DIR, os.path.basename(target_path) or "tts_output.wav")
    success = synthesize_to_file(clean_text, target_path)
    return (target_path if success else ""), False


def release_temp_wav(path: str, was_cached: bool) -> None:
    """Удаляет временный wav после paplay. Файлы .tts_cache не трогает."""
    if was_cached or not path:
        return
    abs_path = os.path.abspath(path)
    cache_root = os.path.abspath(CACHE_DIR) + os.sep
    if abs_path.startswith(cache_root):
        return
    try:
        os.remove(abs_path)
    except OSError:
        pass


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
