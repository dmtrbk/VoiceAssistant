# skills/telegram.py

import os
import shutil
import logging
import subprocess
from skills.base import BaseSkill, RequestContext

class TelegramSkill(BaseSkill):
    """Навык для управления мессенджером Telegram с автоопределением путей и системных пакетов."""

    def __init__(self):
        super().__init__()
        self.open_actions = ["открой", "включи", "запусти"]
        self.close_actions = ["закрой", "выключи", "убери", "выруби", "останови"]
        self.app_names = ["телеграм", "телеграмм", "телега", "telegram"]

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        
        # Проверяем наличие любого из поддерживаемых действий и упоминания Telegram
        has_action = any(action in text for action in (self.open_actions + self.close_actions))
        has_app = any(app_name in text for app_name in self.app_names)
        
        return has_action and has_app

    def _find_telegram_cmd(self) -> list | None:
        """Ищет способ запуска Telegram в системе (локальный путь, pacman/apt, flatpak, snap)."""
        # 1. Проверяем локальный бинарник в домашней папке текущего пользователя
        local_path = os.path.expanduser("~/Telegram/Telegram")
        if os.path.exists(local_path) and os.access(local_path, os.X_OK):
            return [local_path]

        # 2. Проверяем стандартный системный пакет (telegram-desktop)
        if shutil.which("telegram-desktop"):
            return ["telegram-desktop"]

        # 3. Проверяем бинарник Telegram в PATH
        if shutil.which("Telegram"):
            return ["Telegram"]

        # 4. Проверяем Flatpak версию
        if shutil.which("flatpak"):
            try:
                res = subprocess.run(
                    ["flatpak", "info", "org.telegram.desktop"],
                    capture_output=True,
                    text=True,
                    timeout=1.0
                )
                if res.returncode == 0:
                    return ["flatpak", "run", "org.telegram.desktop"]
            except Exception:
                pass

        # 5. Проверяем Snap версию
        if shutil.which("snap"):
            snap_path = "/snap/bin/telegram-desktop"
            if os.path.exists(snap_path) and os.access(snap_path, os.X_OK):
                return [snap_path]

        return None

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        
        # Определяем тип команды: закрытие или открытие
        is_close_command = any(action in text for action in self.close_actions)
        
        if is_close_command:
            context.speak("Закрываю Телеграм.")
            # Мягко завершаем все возможные процессы Telegram в один вызов pkill
            subprocess.Popen(
                ["pkill", "-f", "-i", "telegram"], 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
        else:
            # Пытаемся автоматически найти способ запуска Telegram в системе
            cmd = self._find_telegram_cmd()
            
            if cmd:
                context.speak("Запускаю Телеграм.")
                subprocess.Popen(
                    cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )
            else:
                logging.error(
                    "[Telegram] Не удалось обнаружить Telegram в системе. "
                    "Проверены: домашняя папка, PATH, flatpak, snap."
                )
                context.speak("Я не смогла найти установленный Телеграм в вашей системе.")
