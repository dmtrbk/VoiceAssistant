# skills/utils.py

import os
import logging
import threading
import requests

def _execute_telegram_send(text: str, photo_path: str = None):
    """Внутренний синхронный метод для отправки запроса в Telegram."""
    # Считываем переменные и принудительно очищаем их от лишних кавычек и пробелов
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip('"\'').strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip('"\'').strip()

    if not telegram_bot_token or not telegram_chat_id:
        logging.warning("[Telegram] Ошибка: Токен бота или Chat ID не настроены в файле .env!")
        logging.info(f"[Охрана - Локально] {text}")
        return

    try:
        # 1. Отправка фотографии (при обнаружении движения)
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendPhoto"
            with open(photo_path, 'rb') as photo:
                response = requests.post(
                    url, 
                    data={'chat_id': telegram_chat_id, 'caption': text}, 
                    files={'photo': photo},
                    timeout=10
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
        else:
            logging.error(
                f"[Telegram] Ошибка сервера ({response.status_code}). "
                f"Ответ API: {response.text.strip()}"
            )
            
    except Exception as e:
        logging.error(f"[Telegram] Не удалось связаться с сервером Telegram: {e}")


def send_telegram_notification(text: str, photo_path: str = None, background: bool = True):
    """
    Отправка уведомлений и фотографий безопасности в Telegram с детальной диагностикой.
    
    :param text: Текст уведомления.
    :param photo_path: Путь к отправляемому фото (опционально).
    :param background: Если True (по умолчанию), отправка выполняется в фоновом потоке,
                       чтобы не блокировать синтез речи и работу ассистента.
    """
    if background:
        # Запускаем отправку в отдельном потоке (не блокирует основную программу)
        thread = threading.Thread(
            target=_execute_telegram_send, 
            args=(text, photo_path), 
            daemon=True
        )
        thread.start()
    else:
        # Обычный синхронный запуск
        _execute_telegram_send(text, photo_path)
