# telegram_listener.py

import os
import time
import fcntl
import logging
import threading
import requests
from dotenv import load_dotenv
from commands import execute as execute_command

load_dotenv()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
ALLOWED_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

_listener_lock = threading.Lock()
_is_listener_running = False
_lock_file_handle = None


def _acquire_process_lock() -> bool:
    """Гарантирует, что опрос Telegram getUpdates выполняет только один процесс в ОС."""
    global _lock_file_handle
    lock_path = "/tmp/voiceassistant_telegram.lock"
    try:
        _lock_file_handle = open(lock_path, "w")
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError, OSError):
        return False


def send_reply(chat_id: str, text: str):
    """Отправка текстового ответа пользователю в Telegram."""
    if not TELEGRAM_TOKEN or not text:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": str(chat_id), "text": text}, timeout=10)
    except Exception as e:
        logging.error(f"[Telegram] Ошибка отправки сообщения: {e}")


def run_telegram_listener():
    global _is_listener_running
    with _listener_lock:
        if _is_listener_running:
            return
        if not _acquire_process_lock():
            logging.info("[Telegram] Другой процесс ассистента уже слушает Telegram. Повторный опрос пропущен.")
            return
        _is_listener_running = True

    if not TELEGRAM_TOKEN or not ALLOWED_CHAT_ID:
        logging.error("[Telegram] TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не найдены в .env!")
        return

    offset = 0
    logging.info("[Telegram] Модуль приёма команд запущен.")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            response = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=25).json()

            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()

                # Проверка авторизации отправителя
                if chat_id == str(ALLOWED_CHAT_ID) and text:
                    logging.info(f"[Telegram Command]: {text}")

                    # Callback перехватывает фразы, которые ассистент произносит в reply.
                    # chat_id фиксируем аргументом по умолчанию, иначе замыкание увидит следующее значение из цикла.
                    def telegram_speak(reply_text: str, _chat_id=chat_id):
                        logging.info(f"[Telegram Reply]: {reply_text}")
                        send_reply(_chat_id, reply_text)

                    threading.Thread(
                        target=execute_command,
                        args=(text,),
                        kwargs={"speak_callback": telegram_speak},
                        daemon=True,
                    ).start()

        except Exception as e:
            logging.error(f"[Telegram Error]: {e}")
            time.sleep(5)


def start_telegram_listener_thread():
    """Запускает фоновый поток Telegram-слушателя, если он еще не запущен."""
    t = threading.Thread(target=run_telegram_listener, daemon=True, name="TelegramListenerThread")
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_telegram_listener()
