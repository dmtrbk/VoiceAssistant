# assistant.py

import os
import sys
import logging

from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

import threading
from telegram_listener import run_telegram_listener


# Запуск слушателя Telegram в фоновом режиме
tg_thread = threading.Thread(target=run_telegram_listener, daemon=True)
tg_thread.start()

# Настраиваем логирование первым делом (до импортов)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True
)

# Оптимизация производительности математических библиотек
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import json
import queue
import random
import subprocess
import time
import threading
from difflib import get_close_matches
import numpy as np
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

import sounddevice as sd
from vosk import KaldiRecognizer, Model
import commands
from indicator import run_gui, status_queue
from volume_control import VolumeController
from player_control import emergency_silence
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

WAKE_WORDS = ["джарвис", "умник"]

SAMPLERATE = 16000


def _read_attention_timeout() -> float:
    raw = os.getenv("ATTENTION_TIMEOUT", "6")
    try:
        value = float(raw)
        if value <= 0:
            return 6.0
        return value
    except (TypeError, ValueError):
        return 6.0


def _read_attention_timeout_music() -> float:
    raw = os.getenv("ATTENTION_TIMEOUT_MUSIC", "3")
    try:
        value = float(raw)
        if value <= 0:
            return 3.0
        return value
    except (TypeError, ValueError):
        return 3.0


def _read_min_speech_rms() -> float:
    raw = os.getenv("MIN_SPEECH_RMS", "200")
    try:
        val = float(raw)
        return max(0.0, val)
    except (TypeError, ValueError):
        return 200.0


ATTENTION_TIMEOUT = _read_attention_timeout()
ATTENTION_TIMEOUT_MUSIC = _read_attention_timeout_music()
MIN_SPEECH_RMS = _read_min_speech_rms()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "model")

# Конфигурация Piper TTS
PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL_NAME = os.getenv("PIPER_MODEL", "ru_RU-dmitri-medium.onnx")
PIPER_MODEL = os.path.join(PIPER_DIR, "models", PIPER_MODEL_NAME)

# Оптимизация дискового ввода-вывода (RAM-диск)
if os.path.exists("/dev/shm"):
    TEMP_AUDIO_PATH = "/dev/shm/tts_output.wav"
else:
    TEMP_AUDIO_PATH = os.path.join(BASE_DIR, "tts_output.wav")

VOICE_SPEED = os.getenv("VOICE_SPEED", "1.0")
VOICE_SPEAKER = os.getenv("VOICE_SPEAKER", None)

audio_queue = queue.Queue()

# Блокировка для обеспечения потокобезопасности Vosk
recognizer_lock = threading.Lock()

# Контроллер приглушения звука
volume_ctrl = VolumeController()

# Глобальные переменные состояния
is_speaking = False  
is_thinking = False  # Предотвращает отключение внимания во время обработки ИИ-запроса
playback_interrupted = False
is_active = False
asked_to_repeat = False  # Один мягкий переспрос на сессию при междометии
last_active_time = 0.0
last_speak_end_time = 0.0  # Время окончания речи (для защиты от эхо)
last_spoken_text = ""  # Последняя озвучка — чтобы не принять её за реплику пользователя
play_process = None  # Ссылка на текущий запущенный процесс воспроизведения paplay

# Хвост колонок после paplay и окно, в котором сравниваем STT со своей фразой.
ECHO_TAIL_SEC = 2.2
SELF_ECHO_WINDOW_SEC = ATTENTION_TIMEOUT

ACTIVATION_PHRASES = [
    "Да?",
    "Слушаю вас",
    "Я здесь",
    "Тут я",
    "На связи!",
    "Чем помочь?",
    "Да-да, слушаю",
    "Готов к работе"
]

def clear_audio_queue():
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break

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
    global is_active, asked_to_repeat
    is_active = False
    asked_to_repeat = False
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
    with recognizer_lock:
        recognizer.Reset()
    go_idle()
    logging.info("[Сессия] Аварийная остановка.")


def handle_sleep(recognizer):
    """«спать» / «отбой»: сессия в idle, медиа не трогаем."""
    stop_speaking(to_idle=True)
    with recognizer_lock:
        recognizer.Reset()
    go_idle()
    logging.info("[Сессия] Команда сна. Возврат в ожидание.")


def handle_hold_interrupt(recognizer):
    """«замолчи»: стоп TTS, сессия жива, дакинг не снимаем."""
    stop_speaking(to_idle=False)
    with recognizer_lock:
        recognizer.Reset()
    keep_session_alive()
    logging.info("[Сессия] Прерывание речи, слушаю дальше.")


def speak(text, recognizer=None):
    """Синтезирует аудио в файл на ОЗУ-диске и проигрывает его в асинхронном режиме."""
    global is_speaking, playback_interrupted, play_process, last_active_time, last_speak_end_time
    global last_spoken_text
    if not text:
        return
    
    status_queue.put("speaking")
    is_speaking = True  
    playback_interrupted = False
    last_spoken_text = text
    
    logging.info(f"Ассистент: {text}")
    try:
        cmd = [
            PIPER_EXE, 
            "--model", PIPER_MODEL, 
            "--output_file", TEMP_AUDIO_PATH,
            "--length_scale", VOICE_SPEED  
        ]
        
        if VOICE_SPEAKER is not None:
            cmd.extend(["--speaker", VOICE_SPEAKER])

        piper_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )
        _, stderr = piper_process.communicate(input=text)

        if piper_process.returncode != 0:
            logging.error(f"Ошибка синтеза Piper: {stderr.strip()}")
            is_speaking = False
            status_queue.put("listening" if is_active else "idle")
            return

        if playback_interrupted:
            return

        try:
            play_process = subprocess.Popen(
                ["paplay", TEMP_AUDIO_PATH],
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
                    with recognizer_lock:
                        recognizer.Reset()

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


def get_wake_word(text):
    # Шаг 1: Нормализация типичных фонетических ошибок модели в имена активации (Джарвис / Умник)
    cleaned_text = (
        text.replace("дарвис", "джарвис")
        .replace("сервис", "джарвис")
        .replace("жарис", "джарвис")
        .replace("жарвис", "джарвис")
        .replace("джарвиса", "джарвис")
        .replace("джарвису", "джарвис")
        .replace("джарвисе", "джарвис")
        .replace("джарвисом", "джарвис")
        .replace("джарви", "джарвис")
        .replace("умника", "умник")
        .replace("умнику", "умник")
        .replace("умником", "умник")
    )
    
    for word in WAKE_WORDS:
        if word in cleaned_text:
            return word
            
    # Шаг 2: Нечеткий поиск для обработки прочих звуковых искажений модели
    words = cleaned_text.split()
    if words:
        first_word = words[0]
        matches = get_close_matches(first_word, WAKE_WORDS, n=1, cutoff=0.7)
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

def is_music_leak(phrase_rms: float, detected_wake_word: str | None) -> bool:
    """Тихая фраза без имени активации на фоне музыки — скорее всего текст песни из колонок."""
    if MIN_SPEECH_RMS <= 0:
        return False
    if detected_wake_word:
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
            current_timeout = ATTENTION_TIMEOUT_MUSIC if volume_ctrl.is_ducked_or_playing() else ATTENTION_TIMEOUT
            if time.time() - last_active_time > current_timeout:
                go_idle()
                logging.info(f"[Система] Время ожидания истекло ({current_timeout:g} с). Возврат в спящий режим.")

def main():
    """Основной рабочий цикл ассистента"""
    global is_active, last_active_time, asked_to_repeat

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

            # Игнорируем хвост колонок после своей озвучки, пока он не затихнет.
            if time.time() - last_speak_end_time < ECHO_TAIL_SEC:
                with recognizer_lock:
                    recognizer.Reset()
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

                logging.info(f"[Распознано] {text}")

                detected_wake_word = get_wake_word(text)

                # Фильтр до сессионных команд: иначе «стоп» из текста песни гасит медиа.
                if is_music_leak(phrase_rms, detected_wake_word):
                    logging.info(
                        f"[Аудиофильтр] Отсечена фоновая музыка из колонок "
                        f"(RMS: {phrase_rms:.1f} < {MIN_SPEECH_RMS:g}). Текст: '{text}'"
                    )
                    continue

                # Сессионные команды: авария / сон / стоп TTS. «тишина» сюда не входит.
                if is_emergency_stop(text):
                    handle_emergency_stop(recognizer)
                    continue

                if is_sleep_command(text):
                    handle_sleep(recognizer)
                    continue

                if is_hold_interrupt(text):
                    handle_hold_interrupt(recognizer)
                    continue

                echo_text = text
                if detected_wake_word:
                    echo_text = text.split(detected_wake_word, 1)[-1].strip()
                if echo_text and is_recent_self_echo(echo_text):
                    logging.info(f"[Аудиофильтр] Отсечено эхо своей речи: '{text}'")
                    with recognizer_lock:
                        recognizer.Reset()
                    continue

                # Во время озвучки перебивает только имя. Иначе колонки снова уходят в Groq.
                if is_speaking:
                    if detected_wake_word:
                        stop_speaking(to_idle=False)
                        is_active = True
                        asked_to_repeat = False
                        volume_ctrl.duck()
                    else:
                        with recognizer_lock:
                            recognizer.Reset()
                        continue

                # Работа в активном режиме
                if is_active:
                    if detected_wake_word:
                        phrase = text.split(detected_wake_word, 1)[-1].strip()
                        if phrase:
                            if is_filler(phrase) or is_garbled_utterance(phrase):
                                if not asked_to_repeat:
                                    asked_to_repeat = True
                                    safe_speak("Не расслышал, повторите, пожалуйста")
                                last_active_time = time.time()
                            else:
                                execute_command_async(phrase, safe_speak)
                        else:
                            safe_speak(random.choice(ACTIVATION_PHRASES))
                            last_active_time = time.time()
                    elif is_filler(text) or is_garbled_utterance(text):
                        if not asked_to_repeat:
                            asked_to_repeat = True
                            safe_speak("Не расслышал, повторите, пожалуйста")
                        last_active_time = time.time()
                    else:
                        execute_command_async(text, safe_speak)

                # Работа в спящем режиме
                else:
                    if detected_wake_word:
                        phrase = text.split(detected_wake_word, 1)[-1].strip()

                        is_quick_phrase = bool(phrase) and is_quick_command(phrase)

                        if is_quick_phrase:
                            # Быстрая команда из сна: без удержания сессии (is_active остаётся False)
                            execute_command_async(phrase, safe_speak)
                        else:
                            # Обычная команда — активируем ассистента и приглушаем звук плеера
                            is_active = True
                            asked_to_repeat = False
                            status_queue.put("listening")
                            volume_ctrl.duck()

                            if phrase:
                                if is_filler(phrase) or is_garbled_utterance(phrase):
                                    if not asked_to_repeat:
                                        asked_to_repeat = True
                                        safe_speak("Не расслышал, повторите, пожалуйста")
                                    last_active_time = time.time()
                                else:
                                    execute_command_async(phrase, safe_speak)
                            else:
                                safe_speak(random.choice(ACTIVATION_PHRASES))
                                last_active_time = time.time()
            
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
                    if is_music_leak(current_phrase_max_rms, detected_wake_word):
                        continue

                    if is_emergency_stop(partial_text):
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
                        is_active = True
                        asked_to_repeat = False
                        status_queue.put("listening")
                        volume_ctrl.duck()
                        continue
                else:
                    current_phrase_max_rms = 0.0

if __name__ == "__main__":
    import signal
    import sys

    # Создаем обработчик, который перехватит Ctrl+C и закроет программу чисто
    def sigint_handler(sig, frame):
        logging.info("Ассистент выключен.")
        sys.exit(0)

    # Регистрируем обработчик системного сигнала прерывания (SIGINT / Ctrl+C)
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # 1. Запускаем основной поток распознавания Vosk в фоне
        assistant_thread = threading.Thread(target=main, daemon=True)
        assistant_thread.start()
        
        # 2. На основном потоке запускаем Qt6 GUI
        run_gui()
    except KeyboardInterrupt:
        logging.info("Ассистент выключен.")
