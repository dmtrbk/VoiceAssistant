# assistant.py
# Vosk-цикл не переписывать с нуля. Эхо: mute ECHO_TAIL_SEC + is_self_echo по хвосту фразы
# (Vosk коверкает полный TTS). Во время длинного TTS barge-in только по имени.
# После «Да?» awaiting_followup: короткий хвост и команда без имени, иначе «включи музыку» пропадает.
# OMP_* до numpy. logging до import commands. Не возвращать «Чем помочь?» в ACTIVATION_PHRASES.

import json
import logging
import os
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
from difflib import get_close_matches

from dotenv import load_dotenv

load_dotenv()

# До import numpy: иначе OpenBLAS/MKL съедают CPU на слабом железе.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import numpy as np
import sounddevice as sd
from vosk import KaldiRecognizer, Model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)

import commands
from indicator import run_gui, status_queue
from player_control import emergency_silence
from skills.movie_skill import is_movie_control_phrase, is_movie_playing
from telegram_listener import start_telegram_listener_thread
from volume_control import VolumeController
from tts_cache import get_or_synthesize_wav, precache_common_phrases, SYSTEM_CACHE_PHRASES
from context_manager import clear_active_context, is_in_context
from triggers import (
    is_quick_command,
    is_emergency_stop,
    is_hold_interrupt,
    is_sleep_command,
    is_music_volume_command,
    is_filler,
    is_garbled_utterance,
    is_self_echo,
)

start_telegram_listener_thread()

WAKE_WORDS = ["джарвис", "умник", "гаврила", "гаврюша"]
SAMPLERATE = 16000


def _read_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _read_min_speech_rms() -> float:
    raw = os.getenv("MIN_SPEECH_RMS", "200")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 200.0


ATTENTION_TIMEOUT = _read_positive_float("ATTENTION_TIMEOUT", 6.0)
ATTENTION_TIMEOUT_MUSIC = _read_positive_float("ATTENTION_TIMEOUT_MUSIC", 3.0)
MIN_SPEECH_RMS = _read_min_speech_rms()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "model")

PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL_NAME = os.getenv("PIPER_MODEL", "ru_RU-dmitri-medium.onnx")
PIPER_MODEL = os.path.join(PIPER_DIR, "models", PIPER_MODEL_NAME)

if os.path.exists("/dev/shm"):
    TEMP_AUDIO_PATH = "/dev/shm/tts_output.wav"
else:
    TEMP_AUDIO_PATH = os.path.join(BASE_DIR, "tts_output.wav")

VOICE_SPEED = os.getenv("VOICE_SPEED", "1.0")
VOICE_SPEAKER = os.getenv("VOICE_SPEAKER", None)

audio_queue = queue.Queue()
recognizer_lock = threading.Lock()
volume_ctrl = VolumeController()

is_speaking = False
MUTE_SPEECH = False
is_thinking = False  # пока Groq/навык думает — не гасить сессию по тайм-ауту
playback_interrupted = False
is_active = False
asked_to_repeat = False  # один «не расслышал» на сессию
awaiting_followup = False  # после «Да?» ждём команду; не глушить её хвостом эха
last_active_time = 0.0
last_speak_end_time = 0.0
last_spoken_text = ""
play_process = None

ECHO_TAIL_SEC = 2.2  # после длинного TTS колонки ещё звучат; меньше — снова самодиалог
ECHO_TAIL_AFTER_WAKE_SEC = 0.35  # после «Да?» пользователь сразу говорит команду
SELF_ECHO_WINDOW_SEC = ATTENTION_TIMEOUT

ACTIVATION_PHRASES = [
    "Да?",
    "Слушаю вас",
    "Я здесь",
    "Тут я",
    "На связи!",
    "Да-да, слушаю",
    "Готов к работе",
]

def clear_audio_queue():
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break


def _reset_recognizer(recognizer) -> None:
    if recognizer is None:
        return
    with recognizer_lock:
        recognizer.Reset()


def _strip_wake(text: str, wake: str | None) -> str:
    if not wake:
        return text
    cleaned = normalize_wake_text(text)
    stripped = re.sub(rf"\b{re.escape(wake)}\b", " ", cleaned)
    return re.sub(r"\s+", " ", stripped).strip()


def stop_speaking(to_idle=True):
    """Принудительно останавливает озвучку и очищает аудио-очередь"""
    global is_speaking, playback_interrupted, play_process, last_speak_end_time
    playback_interrupted = True
    is_speaking = False
    last_speak_end_time = time.time()
    
    if play_process is not None:
        try:
            play_process.terminate()
            play_process.wait(timeout=1.0)
        except Exception:
            pass
        play_process = None

    clear_audio_queue()
    if to_idle:
        status_queue.put("idle")


def go_idle():
    """Полный сон сессии: idle, сброс переспроса, музыка обратно."""
    global is_active, asked_to_repeat, awaiting_followup
    is_active = False
    asked_to_repeat = False
    awaiting_followup = False
    clear_active_context()
    status_queue.put("idle")
    volume_ctrl.restore()


def keep_session_alive():
    """Сессия остаётся слушать, таймер внимания обновляется."""
    global is_active, last_active_time
    is_active = True
    last_active_time = time.time()
    status_queue.put("listening")


def handle_emergency_stop(recognizer):
    """«стоп»: гасим TTS, медиа и сессию."""
    stop_speaking(to_idle=True)
    emergency_silence()
    _reset_recognizer(recognizer)
    go_idle()
    logging.info("[Сессия] Аварийная остановка.")


def handle_sleep(recognizer):
    """«спать» / «отбой»: сессия в idle, медиа не трогаем."""
    stop_speaking(to_idle=True)
    _reset_recognizer(recognizer)
    go_idle()
    logging.info("[Сессия] Команда сна. Возврат в ожидание.")


def handle_hold_interrupt(recognizer):
    """«замолчи»: стоп TTS, сессия жива, дакинг не снимаем."""
    stop_speaking(to_idle=False)
    _reset_recognizer(recognizer)
    keep_session_alive()
    logging.info("[Сессия] Прерывание речи, слушаю дальше.")


def speak(text, recognizer=None):
    """Синтезирует аудио (с мгновенной отдачей из TTS-кэша) и проигрывает его в асинхронном режиме."""
    global is_speaking, playback_interrupted, play_process, last_active_time, last_speak_end_time
    global last_spoken_text, MUTE_SPEECH
    if not text:
        return
    
    status_queue.put("speaking")
    is_speaking = True  
    playback_interrupted = False
    last_spoken_text = text
    
    logging.info(f"Ассистент: {text}")

    if MUTE_SPEECH:
        def wait_for_mute():
            global is_speaking, last_active_time, last_speak_end_time
            time.sleep(0.4)
            is_speaking = False
            last_speak_end_time = time.time()
            last_active_time = time.time()
            clear_audio_queue()
            if recognizer:
                _reset_recognizer(recognizer)
            if not playback_interrupted:
                status_queue.put("listening" if is_active else "idle")

        threading.Thread(target=wait_for_mute, daemon=True).start()
        return

    try:
        audio_file, was_cached = get_or_synthesize_wav(text, TEMP_AUDIO_PATH)
        if not audio_file or not os.path.exists(audio_file):
            logging.error("[TTS] Аудиофайл не был создан.")
            is_speaking = False
            status_queue.put("listening" if is_active else "idle")
            return

        if playback_interrupted:
            return

        try:
            play_process = subprocess.Popen(
                ["paplay", audio_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            def wait_for_playback():
                global is_speaking, play_process, last_active_time, last_speak_end_time
                if play_process:
                    play_process.wait()
                play_process = None
                is_speaking = False
                last_speak_end_time = time.time()
                last_active_time = time.time()
                clear_audio_queue()

                if recognizer:
                    _reset_recognizer(recognizer)

                if not playback_interrupted:
                    status_queue.put("listening" if is_active else "idle")

            threading.Thread(target=wait_for_playback, daemon=True).start()
        except Exception as e:
            logging.error(f"[TTS] Ошибка запуска воспроизведения paplay: {e}")
            is_speaking = False
            status_queue.put("listening" if is_active else "idle")
            
    except Exception as e:
        logging.error(f"Ошибка озвучки Piper TTS: {e}")
        is_speaking = False
        status_queue.put("listening" if is_active else "idle")


def is_recent_self_echo(text: str) -> bool:
    """Своя озвучка или её хвост с колонок — не команда."""
    if not last_spoken_text or not text:
        return False
    if is_speaking:
        return is_self_echo(text, last_spoken_text)
    if time.time() - last_speak_end_time > SELF_ECHO_WINDOW_SEC:
        return False
    return is_self_echo(text, last_spoken_text)


_WAKE_FIXES = (
    # Джарвис
    ("дарвис", "джарвис"),
    ("сервис", "джарвис"),
    ("жарис", "джарвис"),
    ("жарвис", "джарвис"),
    ("джарвиса", "джарвис"),
    ("джарвису", "джарвис"),
    ("джарвисе", "джарвис"),
    ("джарвисом", "джарвис"),
    ("джарви", "джарвис"),
    ("джарвиз", "джарвис"),
    ("дярвис", "джарвис"),

    # Умник
    ("умника", "умник"),
    ("умнику", "умник"),
    ("умником", "умник"),
    ("умнике", "умник"),

    # Гаврила
    ("гаврило", "гаврила"),
    ("гаврилы", "гаврила"),
    ("гавриле", "гаврила"),
    ("гаврилу", "гаврила"),
    ("гаврилой", "гаврила"),
    ("гаврилом", "гаврила"),
    ("гаврилла", "гаврила"),
    ("гаврилка", "гаврила"),
    ("гаврик", "гаврила"),
    ("гаврика", "гаврила"),
    ("гаврику", "гаврила"),
    ("гавриком", "гаврила"),
    ("гавриил", "гаврила"),
    ("гавриила", "гаврила"),
    ("гавриилу", "гаврила"),
    ("гавриилом", "гаврила"),
    ("гав рила", "гаврила"),
    ("гав рилу", "гаврила"),
    ("гав рило", "гаврила"),
    ("говорила", "гаврила"),
    ("говорили", "гаврила"),
    ("говорило", "гаврила"),
    ("горилла", "гаврила"),
    ("горила", "гаврила"),

    # Гаврюша (ошибки Vosk из-за отсутствия в базовом словаре)
    ("гав рюша", "гаврюша"),
    ("гав рюше", "гаврюша"),
    ("гав рюшу", "гаврюша"),
    ("гав рюши", "гаврюша"),
    ("гав рюшей", "гаврюша"),
    ("гав рюш", "гаврюша"),
    ("гав руша", "гаврюша"),
    ("гав руше", "гаврюша"),
    ("гав рушу", "гаврюша"),
    ("гав руши", "гаврюша"),
    ("гав руш", "гаврюша"),
    ("гав душа", "гаврюша"),
    ("гав душе", "гаврюша"),
    ("гав душу", "гаврюша"),
    ("гав люша", "гаврюша"),
    ("гав люше", "гаврюша"),
    ("гав люшу", "гаврюша"),
    ("гав юша", "гаврюша"),
    ("гав уша", "гаврюша"),
    ("гав уши", "гаврюша"),
    ("гаврюше", "гаврюша"),
    ("гаврюшу", "гаврюша"),
    ("гаврюши", "гаврюша"),
    ("гаврюшей", "гаврюша"),
    ("гаврюш", "гаврюша"),
    ("гавруша", "гаврюша"),
    ("гавруше", "гаврюша"),
    ("гаврушу", "гаврюша"),
    ("гавруш", "гаврюша"),
    ("гарюша", "гаврюша"),
    ("гарюше", "гаврюша"),
    ("гарюшу", "гаврюша"),
    ("горюша", "гаврюша"),
    ("горюше", "гаврюша"),
    ("горюшу", "гаврюша"),
    ("говорю же", "гаврюша"),
    ("говори уже", "гаврюша"),
    ("говорит уже", "гаврюша"),
    ("говори же", "гаврюша"),
    ("говори уж", "гаврюша"),
    ("говорить уже", "гаврюша"),
    ("лавроша", "гаврюша"),
    ("лавруша", "гаврюша"),
    ("гаврюк", "гаврюша"),
    ("глафира", "гаврюша"),
    ("глафиру", "гаврюша"),
)


def normalize_wake_text(text: str) -> str:
    cleaned = (text or "").lower().replace("ё", "е").strip()
    # Сначала составные ошибки с пробелами
    for src, dst in _WAKE_FIXES:
        if " " in src and src in cleaned:
            cleaned = cleaned.replace(src, dst)
    # Затем одиночные слова
    for src, dst in _WAKE_FIXES:
        if " " not in src:
            cleaned = re.sub(rf"\b{re.escape(src)}\b", dst, cleaned)
    return cleaned


def get_wake_word(text: str) -> str | None:
    cleaned_text = normalize_wake_text(text)

    for word in WAKE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", cleaned_text):
            return word

    words = cleaned_text.split()
    if words:
        matches = get_close_matches(words[0], WAKE_WORDS, n=1, cutoff=0.72)
        if matches:
            return matches[0]
    return None


def execute_command_async(cmd_text, safe_speak_func):
    """Асинхронный запуск выполнения команды в фоновом потоке, чтобы не блокировать STТ."""
    def run():
        global is_thinking, last_active_time, is_active

        was_active = is_active
        is_quick = is_quick_command(cmd_text)

        # Если команда быстрая, передаем пустую функцию вместо озвучки (блокируем синтез Piper)
        active_speak = (lambda text: None) if is_quick else safe_speak_func

        status_queue.put("thinking")
        is_thinking = True

        should_sleep = commands.execute(cmd_text, active_speak)

        is_thinking = False
        last_active_time = time.time()

        if should_sleep:
            go_idle()
            logging.info("[Сессия] Прощание. Уход в сон.")
            return

        # Быстрая команда не гасит сессию, если она уже была активна.
        # Громкость плеера восстанавливаем только при уходе в idle.
        if is_quick:
            if was_active:
                is_active = True
                last_active_time = time.time()
                if not is_speaking:
                    status_queue.put("listening")
            else:
                is_active = False
                status_queue.put("idle")
                if not is_music_volume_command(cmd_text):
                    volume_ctrl.restore()
        else:
            if not is_speaking:
                status_queue.put("listening" if is_active else "idle")

    threading.Thread(target=run, daemon=True).start()


def is_music_leak(
    phrase_rms: float,
    detected_wake_word: str | None,
    text: str = "",
) -> bool:
    """Речь с колонок: тихая песня или громкий диалог фильма без имени активации."""
    if detected_wake_word:
        return False
    if is_movie_playing():
        # Фильм говорит громко — порог RMS его не отсекает. Без «Джарвис» не слушаем,
        # кроме короткого пульта в уже открытой сессии («пауза», «закрой»).
        if is_active and is_movie_control_phrase(text):
            return False
        return True
    if MIN_SPEECH_RMS <= 0:
        return False
    if not volume_ctrl.is_ducked_or_playing():
        return False
    return phrase_rms < MIN_SPEECH_RMS


def timeout_monitor():
    """Фоновый мониторинг времени ожидания команды с учетом воспроизведения музыки."""
    global is_active, is_speaking, is_thinking, last_active_time
    while True:
        time.sleep(0.5)
        # Проверяем тайм-аут, только если активны, НЕ говорим и НЕ ожидаем ответ от ИИ (заморозка таймера)
        if is_active and not is_speaking and not is_thinking:
            media_on = volume_ctrl.is_ducked_or_playing() or is_movie_playing()
            current_timeout = ATTENTION_TIMEOUT_MUSIC if media_on else ATTENTION_TIMEOUT
            if time.time() - last_active_time > current_timeout:
                go_idle()
                logging.info(f"[Система] Время ожидания истекло ({current_timeout:g} с). Возврат в спящий режим.")


def main():
    """Основной рабочий цикл ассистента"""
    global is_active, last_active_time, asked_to_repeat, awaiting_followup

    if not os.path.exists(MODEL_PATH):
        logging.critical(f"Папка с моделью Vosk не найдена по пути: {MODEL_PATH}")
        return
        
    if not os.path.exists(PIPER_EXE):
        logging.critical(f"Исполняемый файл Piper не найден по пути: {PIPER_EXE}.")
        return
    if not os.path.exists(PIPER_MODEL):
        logging.critical(f"Модель Piper не найдена по пути: {PIPER_MODEL}.")
        return

    vosk_model = Model(MODEL_PATH)
    recognizer = KaldiRecognizer(vosk_model, SAMPLERATE)
    
    logging.info(
        f"[Система] Ассистент готов. Позовите: {', '.join(WAKE_WORDS)}. "
        f"Тайм-аут внимания: {ATTENTION_TIMEOUT:g} с (при музыке: {ATTENTION_TIMEOUT_MUSIC:g} с)."
    )
    is_active = False
    asked_to_repeat = False
    last_active_time = 0.0

    def safe_speak(text):
        speak(text, recognizer)

    def prompt_repeat():
        # Не плодить «не расслышал» на каждый шум в одной сессии.
        global asked_to_repeat, last_active_time
        if not asked_to_repeat:
            asked_to_repeat = True
            safe_speak("Не расслышал, повторите, пожалуйста")
        last_active_time = time.time()

    def dispatch_phrase(phrase: str) -> None:
        global awaiting_followup
        if is_filler(phrase) or is_garbled_utterance(phrase):
            prompt_repeat()
        else:
            awaiting_followup = False
            execute_command_async(phrase, safe_speak)

    def speak_activation() -> None:
        global awaiting_followup, last_active_time
        awaiting_followup = True
        last_active_time = time.time()
        phrase = random.choice(ACTIVATION_PHRASES)
        # Не блокировать цикл Vosk синтезом «Да?» — иначе следующая команда сидит в очереди и глохнет.
        threading.Thread(target=safe_speak, args=(phrase,), daemon=True).start()

    def wake_session() -> None:
        global is_active, asked_to_repeat
        is_active = True
        asked_to_repeat = False
        status_queue.put("listening")
        volume_ctrl.duck()

    def audio_callback(indata, frames, time_info, status):
        audio_queue.put(bytes(indata))

    # Запускаем фоновый монитор тайм-аута внимания
    threading.Thread(target=timeout_monitor, daemon=True).start()

    current_phrase_max_rms = 0.0

    with sd.RawInputStream(samplerate=SAMPLERATE, blocksize=2000, dtype="int16", channels=1, callback=audio_callback):
        while True:
            data = audio_queue.get()

            # Уровень энергии текущего аудио-фрейма
            chunk_samples = np.frombuffer(data, dtype=np.int16)
            chunk_rms = float(np.sqrt(np.mean(chunk_samples.astype(np.float32) ** 2))) if len(chunk_samples) > 0 else 0.0

            # После «Да?» короткий хвост: иначе «включи музыку» съедается Reset-ом Vosk.
            echo_tail = ECHO_TAIL_AFTER_WAKE_SEC if awaiting_followup else ECHO_TAIL_SEC
            if time.time() - last_speak_end_time < echo_tail:
                _reset_recognizer(recognizer)
                current_phrase_max_rms = 0.0
                continue

            # 1. Обработка завершенных реплик с блокировкой
            is_accepted = False
            with recognizer_lock:
                is_accepted = recognizer.AcceptWaveform(data)

            if is_accepted:
                # Последний чанк фразы тоже входит в оценку громкости
                phrase_rms = max(current_phrase_max_rms, chunk_rms)
                current_phrase_max_rms = 0.0

                with recognizer_lock:
                    res = json.loads(recognizer.Result())
                text = res.get("text", "").lower().strip()
                if not text:
                    continue

                detected_wake_word = get_wake_word(text)

                # Фильтр до сессионных команд: иначе «стоп» из текста песни гасит медиа.
                if is_music_leak(phrase_rms, detected_wake_word, text):
                    logging.info(
                        f"[Аудиофильтр] Отсечена речь колонок "
                        f"(RMS: {phrase_rms:.1f}). Текст: '{text}'"
                    )
                    continue

                logging.info(f"[Распознано] {text}")

                # Сессионные команды: авария / сон / стоп TTS. «тишина» сюда не входит.
                if is_emergency_stop(text):
                    if is_in_context():
                        phrase = _strip_wake(text, detected_wake_word) or text
                        stop_speaking(to_idle=False)
                        keep_session_alive()
                        execute_command_async(phrase, safe_speak)
                    else:
                        handle_emergency_stop(recognizer)
                    continue

                if is_sleep_command(text):
                    handle_sleep(recognizer)
                    continue

                if is_hold_interrupt(text):
                    handle_hold_interrupt(recognizer)
                    continue

                echo_text = _strip_wake(text, detected_wake_word)
                if echo_text and is_recent_self_echo(echo_text):
                    logging.info(f"[Аудиофильтр] Отсечено эхо своей речи: '{text}'")
                    _reset_recognizer(recognizer)
                    continue

                # Во время озвучки: имя всегда; после «Да?» — ещё и команда без имени.
                if is_speaking:
                    if detected_wake_word:
                        stop_speaking(to_idle=False)
                        wake_session()
                    elif awaiting_followup:
                        stop_speaking(to_idle=False)
                    else:
                        _reset_recognizer(recognizer)
                        continue

                phrase = _strip_wake(text, detected_wake_word) if detected_wake_word else text

                if is_active:
                    if detected_wake_word and not phrase:
                        speak_activation()
                    else:
                        dispatch_phrase(phrase)
                elif detected_wake_word:
                    if phrase and is_quick_command(phrase):
                        execute_command_async(phrase, safe_speak)
                    else:
                        wake_session()
                        if phrase:
                            dispatch_phrase(phrase)
                        else:
                            speak_activation()
            
            # 2. Обработка промежуточных результатов для мгновенного прерывания с блокировкой
            else:
                with recognizer_lock:
                    partial_res = json.loads(recognizer.PartialResult())
                partial_text = partial_res.get("partial", "").lower().strip()
                
                if partial_text:
                    # Копим RMS только пока Vosk видит речь, а не паузы и музыку между фразами
                    current_phrase_max_rms = max(current_phrase_max_rms, chunk_rms)

                    # Короткие обрывки — шум или эхо, не трогаем сессию
                    if len(partial_text) < 4:
                        continue

                    detected_wake_word = get_wake_word(partial_text)
                    if is_music_leak(current_phrase_max_rms, detected_wake_word, partial_text):
                        continue

                    if is_emergency_stop(partial_text):
                        if not is_in_context():
                            handle_emergency_stop(recognizer)
                        continue

                    if is_sleep_command(partial_text):
                        handle_sleep(recognizer)
                        continue

                    if is_hold_interrupt(partial_text):
                        handle_hold_interrupt(recognizer)
                        continue

                    # Если ассистент говорит и услышал имя активации, мгновенно останавливаем речь
                    if is_speaking and detected_wake_word:
                        stop_speaking(to_idle=False)
                        wake_session()
                        continue
                else:
                    current_phrase_max_rms = 0.0


if __name__ == "__main__":
    import argparse
    from cli import run_cli_with_gui, run_interactive_loop

    parser = argparse.ArgumentParser(description="Голосовой ассистент Джарвис (Manjaro GNOME)")
    parser.add_argument("-c", "--cli", action="store_true", help="Запустить в текстовом консольном режиме (CLI)")
    parser.add_argument("--no-gui", "--headless", action="store_true", help="Запустить голосовой ассистент без графического виджета-сферы")
    parser.add_argument("-m", "--mute", action="store_true", help="Отключить динамики (тихий режим)")
    parser.add_argument("-d", "--debug", "-v", "--verbose", action="store_true", help="Включить подробный режим отладки (DEBUG logging)")

    args, unknown = parser.parse_known_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.mute:
        MUTE_SPEECH = True

    # Проверка переменных окружения
    env_cli = os.getenv("CONSOLE_MODE", "false").lower() in ["1", "true", "yes"]
    env_no_gui = os.getenv("GUI_ENABLED", "true").lower() in ["0", "false", "no"]

    if args.cli or env_cli:
        if args.no_gui or env_no_gui:
            run_interactive_loop(mute=args.mute, verbose=args.debug)
        else:
            run_cli_with_gui(mute=args.mute, verbose=args.debug)
        sys.exit(0)

    # Создаем обработчик, который перехватит Ctrl+C и закроет программу чисто
    def sigint_handler(sig, frame):
        logging.info("Ассистент выключен.")
        sys.exit(0)

    # Регистрируем обработчик системного сигнала прерывания (SIGINT / Ctrl+C)
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # Фоновый прогрев кэша частых фраз
        threading.Thread(
            target=lambda: precache_common_phrases(
                list(dict.fromkeys(ACTIVATION_PHRASES + SYSTEM_CACHE_PHRASES))
            ),
            daemon=True
        ).start()

        if args.no_gui or env_no_gui:
            logging.info("[Система] Запуск в headless-режиме (без GUI)...")
            main()
        else:
            # 1. Запускаем основной поток распознавания Vosk в фоне
            assistant_thread = threading.Thread(target=main, daemon=True)
            assistant_thread.start()
            
            # 2. На основном потоке запускаем Qt6 GUI
            run_gui()
    except KeyboardInterrupt:
        logging.info("Ассистент выключен.")
