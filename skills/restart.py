import os
import sys
import subprocess
import threading
import time
import logging

from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)


class RestartSkill(BaseSkill):
    def __init__(self):
        self.triggers = [
            "перезагрузись",
            "перезапустись",
            "перезагрузи ассистента",
            "перезапусти ассистента",
            "рестарт",
        ]

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        return any(trigger in text for trigger in self.triggers)

    def execute(self, context: RequestContext) -> None:
        context.speak("Перезагружаюсь.")
        threading.Thread(target=self._deferred_restart, daemon=True).start()

    def _deferred_restart(self):
        time.sleep(1.5)
        try:
            logger.info("Перезапуск службы voice-assistant.service...")
            subprocess.run(
                ["systemctl", "--user", "restart", "voice-assistant.service"],
                check=True,
            )
        except Exception as exc:
            logger.error(f"Ошибка systemctl: {exc}. Применяем os.execv.")
            os.execv(sys.executable, [sys.executable] + sys.argv)
