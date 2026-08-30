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
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

import sounddevice as sd
from vosk import KaldiRecognizer, Model
import commands
from indicator import run_gui, status_queue
from volume_control import VolumeController

WAKE_WORDS = ["джарвис", "дарвис", "сервис", "жарис", "умник"]

SAMPLERATE = 16000
ATTENTION_TIMEOUT = 4
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "model")

# Конфигурация Piper TTS
PIPER_DIR = os.path.join(BASE_DIR, "piper")
PIPER_EXE = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL = os.path.join(PIPER_DIR, "models", "ru_RU-dmitri-medium.onnx")

# Оптимизация дискового ввода-вывода (RAM-диск)
if os.path.exists("/dev/shm"):
    TEMP_AUDIO_PATH = "/dev/shm/tts_output.wav"
else:
    TEMP_AUDIO_PATH = os.path.join(BASE_DIR, "tts_output.wav")

VOICE_SPEED = "0.95" 
VOICE_SPEAKER = None 

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
last_active_time = 0.0
last_speak_end_time = 0.0  # Время окончания речи (для защиты от эхо)
play_process = None  # Ссылка на текущий запущенный процесс воспроизведения paplay

ACTIVATION_PHRASES = [
    "Да, я слушаю.",
    "На связи.",
    "Да, повелитель.",
    "Я тут.",
    "Слушаю вас.",
    "Чем помочь?",
    "Я готов.",
    "Да-да!"
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

def speak(text, recognizer=None):
    """Синтезирует аудио в файл на ОЗУ-диске и проигрывает его в асинхронном режиме."""
    global is_speaking, playback_interrupted, play_process, last_active_time, last_speak_end_time
    if not text:
        return
    
    status_queue.put("speaking")
    is_speaking = True  
    playback_interrupted = False  
    
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
                last_speak_end_time = time.time()  # Фиксируем точное время окончания воспроизведения
                last_active_time = time.time()  # Тайм-аут внимания отсчитывается после конца фразы

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

def get_wake_word(text):
    # Шаг 1: Нормализация типичных фонетических ошибок модели в слово "джарвис"
    cleaned_text = text.replace("дарвис", "джарвис").replace("сервис", "джарвис").replace("жарис", "джарвис")
    
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
        
        # Полный список триггеров для беззвучных быстрых команд
        quick_triggers = [
            "громче", "тише", "громкость плюс", "громкость минус",
            "следующий", "предыдущий", "вперед", "назад", "дальше", "прошлый трек", "следующий трек", "трек",
            "пауза", "плей", "играй", "возобнови",
            "выключи музыку", "выруби музыку", "останови музыку", "тишина"
        ]
        is_quick = any(w in cmd_text for w in quick_triggers)
        
        # Если команда быстрая, передаем пустую функцию вместо озвучки (блокируем синтез Piper)
        active_speak = (lambda text: None) if is_quick else safe_speak_func
        
        status_queue.put("thinking")
        is_thinking = True
        
        # Выполняем команду
        commands.execute(cmd_text, active_speak)
        
        is_thinking = False
        last_active_time = time.time()
        
        # После выполнения сбрасываем активность и восстанавливаем состояние
        if is_quick:
            is_active = False
            status_queue.put("idle")
            
            # --- ЗАЩИТА: Не сбрасываем громкость плеера, если мы только что её настраивали ---
            is_music_volume_cmd = any(w in cmd_text for w in ["громче", "тише", "громкость"]) and any(w in cmd_text for w in ["музыка", "музыку", "плеер"])
            if not is_music_volume_cmd:
                volume_ctrl.restore()
        else:
            if not is_speaking:
                status_queue.put("listening" if is_active else "idle")
                
    threading.Thread(target=run, daemon=True).start()

def timeout_monitor():
    """Фоновый мониторинг времени ожидания команды."""
    global is_active, is_speaking, is_thinking, last_active_time
    while True:
        time.sleep(0.5)
        # Проверяем тайм-аут, только если активны, НЕ говорим и НЕ ожидаем ответ от ИИ (заморозка таймера)
        if is_active and not is_speaking and not is_thinking:
            if time.time() - last_active_time > ATTENTION_TIMEOUT:
                is_active = False
                status_queue.put("idle")
                logging.info("[Система] Время ожидания истекло. Возврат в спящий режим.")
                volume_ctrl.restore()  # Восстанавливаем громкость музыки при сне

def main():
    """Основной рабочий цикл ассистента"""
    global is_active, last_active_time

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
    
    logging.info(f"[Система] Ассистент готов. Позовите: {', '.join(WAKE_WORDS)}")
    is_active = False
    last_active_time = 0.0

    def safe_speak(text):
        speak(text, recognizer)

    def audio_callback(indata, frames, time_info, status):
        audio_queue.put(bytes(indata))

    # Запускаем фоновый монитор тайм-аута внимания
    threading.Thread(target=timeout_monitor, daemon=True).start()

    with sd.RawInputStream(samplerate=SAMPLERATE, blocksize=2000, dtype="int16", channels=1, callback=audio_callback):
        while True:
            data = audio_queue.get()

            # --- ЗАЩИТА ОТ ЭХО (ШЛЕЙФА) ---
            # Игнорируем входящие звуки в течение 0.2 сек после фразы (устраняет задержку перед ответом)
            if time.time() - last_speak_end_time < 0.2:
                with recognizer_lock:
                    recognizer.Reset()
                continue

            # 1. Обработка завершенных реплик с блокировкой
            is_accepted = False
            with recognizer_lock:
                is_accepted = recognizer.AcceptWaveform(data)

            if is_accepted:
                with recognizer_lock:
                    res = json.loads(recognizer.Result())
                text = res.get("text", "").lower().strip()
                if not text:
                    continue

                # Срочная остановка всегда в приоритете (гасит речь, loopback, audacious и Glava)
                if "стоп" in text or "замолчи" in text:
                    stop_speaking(to_idle=True)
                    
                    # Мягкое выключение Audacious и Glava через скрипт пользователя
                    script_off = os.path.expanduser("~/.scripts/player_off.sh")
                    if os.path.exists(script_off):
                        subprocess.Popen(["systemd-run", "--user", "--scope", "bash", script_off], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.Popen(["audtool", "--playback-stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.Popen(["pkill", "audacious"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.Popen(["pkill", "glava"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    subprocess.Popen(["pactl", "unload-module", "module-loopback"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    with recognizer_lock:
                        recognizer.Reset()
                    is_active = False
                    volume_ctrl.restore()  # Восстанавливаем оригинальную громкость
                    continue

                detected_wake_word = get_wake_word(text)

                # Во время озвучки игнорируем любые фразы, кроме тех, что содержат имя активации
                if is_speaking:
                    if detected_wake_word:
                        stop_speaking(to_idle=False)
                        is_active = True
                        volume_ctrl.duck()  # Приглушаем при переактивации
                    else:
                        continue

                # Работа в активном режиме
                if is_active:
                    if detected_wake_word:
                        phrase = text.split(detected_wake_word, 1)[-1].strip()
                        if phrase:
                            execute_command_async(phrase, safe_speak)
                        else:
                            safe_speak(random.choice(ACTIVATION_PHRASES))
                            last_active_time = time.time()
                    else:
                        execute_command_async(text, safe_speak)

                # Работа в спящем режиме
                else:
                    if detected_wake_word:
                        phrase = text.split(detected_wake_word, 1)[-1].strip()
                        
                        # Проверяем, является ли произнесенная фраза быстрой командой
                        quick_triggers = [
                            "громче", "тише", "громкость плюс", "громкость минус",
                            "следующий", "предыдущий", "вперед", "назад", "дальше", "прошлый трек", "следующий трек", "трек",
                            "пауза", "плей", "играй", "возобнови",
                            "выключи музыку", "выруби музыку", "останови музыку", "тишина"
                        ]
                        is_quick_phrase = phrase and any(w in phrase for w in quick_triggers)
                        
                        if is_quick_phrase:
                            # Быстрая команда из сна выполняется без приглушения и без удержания активности
                            execute_command_async(phrase, safe_speak)
                        else:
                            # Обычная команда — активируем ассистента и приглушаем звук плеера
                            is_active = True
                            status_queue.put("listening")  
                            volume_ctrl.duck()
                            
                            if phrase:
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
                    if "стоп" in partial_text or "замолчи" in partial_text:
                        stop_speaking(to_idle=True)
                        
                        # Мягкое выключение Audacious и Glava через скрипт пользователя
                        script_off = os.path.expanduser("~/.scripts/player_off.sh")
                        if os.path.exists(script_off):
                            subprocess.Popen(["systemd-run", "--user", "--scope", "bash", script_off], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        else:
                            subprocess.Popen(["audtool", "--playback-stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.Popen(["pkill", "audacious"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.Popen(["pkill", "glava"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        subprocess.Popen(["pactl", "unload-module", "module-loopback"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        with recognizer_lock:
                            recognizer.Reset()
                        is_active = False
                        volume_ctrl.restore()  # Восстанавливаем оригинальную громкость
                        continue

                    # Если ассистент говорит и услышал имя активации, мгновенно останавливаем речь
                    detected_wake_word = get_wake_word(partial_text)
                    if is_speaking and detected_wake_word:
                        stop_speaking(to_idle=False)
                        is_active = True
                        status_queue.put("listening")
                        volume_ctrl.duck()  # Приглушаем при переактивации
                        continue

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
