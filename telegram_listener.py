# telegram_listener.py

import html
import os
import queue
import re
import time
import fcntl
import logging
import threading
import requests
from dotenv import load_dotenv
from commands import execute as execute_command

load_dotenv()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip().strip("\"'")
ALLOWED_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip().strip("\"'")

_listener_lock = threading.Lock()
_is_listener_running = False
_lock_file_handle = None
_command_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


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


def _to_telegram_html(text: str) -> str:
    """Экранирует HTML и превращает простой Markdown Groq в теги Telegram."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def send_reply(chat_id: str, text: str):
    """Отправка текстового ответа пользователю в Telegram."""
    if not TELEGRAM_TOKEN or not text:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": _to_telegram_html(text),
        "parse_mode": "HTML",
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code >= 400:
            requests.post(
                url,
                json={"chat_id": str(chat_id), "text": text},
                timeout=10,
            )
    except Exception as e:
        logging.error(f"[Telegram] Ошибка отправки сообщения: {e}")


def _command_worker() -> None:
    """Один поток на все входящие: не плодим Thread на каждое сообщение."""
    while True:
        item = _command_queue.get()
        if item is None:
            return
        text, chat_id = item

        def telegram_speak(reply_text: str, _chat_id=chat_id) -> None:
            logging.info("[Telegram] Ответ отправлен (%s симв.).", len(reply_text or ""))
            send_reply(_chat_id, reply_text)

        try:
            execute_command(text, speak_callback=telegram_speak, channel="telegram")
        except Exception as exc:
            logging.error("[Telegram] Ошибка выполнения: %s", exc)


def _ensure_command_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_command_worker, daemon=True, name="TelegramCommandWorker").start()


def enqueue_telegram_command(text: str, chat_id: str) -> None:
    """Кладёт команду в очередь единственного worker (удобно тестировать)."""
    _ensure_command_worker()
    _command_queue.put((text, chat_id))


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
        with _listener_lock:
            _is_listener_running = False
        return

    _ensure_command_worker()
    offset = 0
    logging.info("[Telegram] Модуль приёма команд запущен.")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            response = requests.get(
                url,
                params={"offset": offset, "timeout": 20},
                timeout=(5, 30),
            )
            try:
                payload = response.json()
            except ValueError:
                logging.error("[Telegram] getUpdates вернул не JSON (HTTP %s).", response.status_code)
                time.sleep(5)
                continue
            if not payload.get("ok"):
                logging.error("[Telegram] getUpdates: %s", payload.get("description") or "unknown")
                time.sleep(5)
                continue

            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()

                if chat_id == str(ALLOWED_CHAT_ID) and text:
                    logging.info("[Telegram] Команда принята (%s симв.).", len(text))
                    enqueue_telegram_command(text, chat_id)

        except requests.Timeout:
            logging.debug("[Telegram] long poll без обновлений, жду дальше.")
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
