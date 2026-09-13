# skills/utils.py

import os
import logging
import threading
import requests

def telegram_configured() -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip('"\'').strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip('"\'').strip()
    return bool(token and chat_id)


def _execute_telegram_send(text: str, photo_path: str = None) -> bool:
    """Внутренний синхронный метод для отправки запроса в Telegram."""
    # Считываем переменные и принудительно очищаем их от лишних кавычек и пробелов
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip('"\'').strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip('"\'').strip()

    if not telegram_bot_token or not telegram_chat_id:
        logging.warning("[Telegram] Ошибка: Токен бота или Chat ID не настроены в файле .env!")
        logging.info(f"[Охрана - Локально] {text}")
        return False

    try:
        # 1. Отправка фотографии (охрана или сгенерированная картинка)
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendPhoto"
            with open(photo_path, 'rb') as photo:
                response = requests.post(
                    url,
                    data={'chat_id': telegram_chat_id, 'caption': text},
                    files={'photo': photo},
                    timeout=30
                )
        # 2. Отправка текстового сообщения
        else:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            response = requests.post(
                url,
                data={'chat_id': telegram_chat_id, 'text': text},
                timeout=10
            )

        # Проверяем ответ сервера Telegram
        if response.status_code == 200:
            logging.info("[Telegram] Уведомление успешно отправлено.")
            return True
        logging.error("[Telegram] Ошибка сервера (%s).", response.status_code)
        return False

    except Exception as e:
        logging.error(f"[Telegram] Не удалось связаться с сервером Telegram: {e}")
        return False


def send_telegram_notification(text: str, photo_path: str = None, background: bool = True) -> bool:
    """
    Отправка уведомлений и фотографий в Telegram с детальной диагностикой.

    :param text: Текст уведомления.
    :param photo_path: Путь к отправляемому фото (опционально).
    :param background: Если True (по умолчанию), отправка выполняется в фоновом потоке,
                       чтобы не блокировать синтез речи и работу ассистента.
    :return: Для фоновой отправки True, если поток запущен. Иначе результат HTTP-запроса.
    """
    if background:
        # Запускаем отправку в отдельном потоке (не блокирует основную программу)
        thread = threading.Thread(
            target=_execute_telegram_send,
            args=(text, photo_path),
            daemon=True
        )
        thread.start()
        return True
    return _execute_telegram_send(text, photo_path)
