import os
import sys
import subprocess
import threading
import time
import logging

logger = logging.getLogger(__name__)

class RestartSkill:
    def __init__(self):
        self.triggers = [
            "перезагрузись",
            "перезапустись",
            "перезагрузи ассистента",
            "перезапусти ассистента",
            "рестарт"
        ]

    def _extract_text(self, ctx_or_text) -> str:
        """Извлекает строку из RequestContext или использует входящий текст."""
        if isinstance(ctx_or_text, str):
            return ctx_or_text
        if hasattr(ctx_or_text, 'text'):
            return str(ctx_or_text.text)
        elif hasattr(ctx_or_text, 'raw_text'):
            return str(ctx_or_text.raw_text)
        return str(ctx_or_text)

    def can_handle(self, context) -> bool:
        """Проверяет триггеры перезагрузки."""
        text = self._extract_text(context).lower().strip()
        return any(trigger in text for trigger in self.triggers)

    def execute(self, context, voice_assistant=None) -> str:
        """Синхронный метод обработки команды."""
        # Запускаем перезапуск в отдельном потоке с задержкой, 
        # чтобы озвучка успела проиграть ответ "Перезагружаюсь..."
        threading.Thread(target=self._deferred_restart, daemon=True).start()
        return "Перезагружаюсь..."

    def _deferred_restart(self):
        """Пауза перед перезапуском для завершения фразы."""
        time.sleep(1.5)
        try:
            logger.info("Перезапуск службы voice-assistant.service...")
            subprocess.run(["systemctl", "--user", "restart", "voice-assistant.service"], check=True)
        except Exception as e:
            logger.error(f"Ошибка systemctl: {e}. Применяем os.execv.")
            os.execv(sys.executable, [sys.executable] + sys.argv)
