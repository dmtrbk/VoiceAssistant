# telegram_listener.py

import os
import time
import logging
import requests
from dotenv import load_dotenv
from commands import execute as execute_command

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


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

                    # Callback перехватывает фразы, которые ассистент произносит в reply
                    def telegram_speak(reply_text: str):
                        logging.info(f"[Telegram Reply]: {reply_text}")
                        send_reply(chat_id, reply_text)

                    # Передаем команду и коллбэк в маршрутизатор
                    execute_command(text, speak_callback=telegram_speak)

        except Exception as e:
            logging.error(f"[Telegram Error]: {e}")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_telegram_listener()
