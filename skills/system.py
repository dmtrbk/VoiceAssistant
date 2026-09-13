# skills/system.py

import random
import logging
import subprocess
import shutil
import time
from skills.base import BaseSkill, RequestContext
from window_control import (
    close_all_windows,
    close_focused_window,
    detect_window_action,
    show_desktop,
)
from browser import chrome_command, close_browser, is_close_browser_text

SUCCESS_RESPONSES = [
    "Сделано.", "Готово.", "Есть.",
]

class SystemSkill(BaseSkill):
    """Навык для управления операционной системой Linux (громкость, утилиты, выключение)."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        triggers = [
            "терминал", "файлы", "проводник", "настройки",
            "громче", "громкость плюс", "тише", "громкость минус",
            "системный монитор", "шахматы", "chess",
        ]
        is_shutdown = any(w in text for w in ["выключ", "отключ"]) and any(w in text for w in ["компьютер", "пк"])
        return (
            any(w in text for w in triggers)
            or is_shutdown
            or is_close_browser_text(text)
            or detect_window_action(text) is not None
        )

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        window_action = detect_window_action(text)
        if window_action == "show_desktop":
            if show_desktop():
                context.speak("Сворачиваю.")
            else:
                context.speak("Не вышло.")
            return

        if window_action == "close_focused":
            if close_focused_window():
                context.speak("Закрываю.")
            else:
                context.speak("Не вышло.")
            return

        if window_action == "close_all":
            logging.info("[Система] Закрываю все окна.")
            closed = close_all_windows()
            if closed:
                context.speak("Закрываю.")
            else:
                context.speak("Не нашёл.")
            return

        # Закрытие и остановка приложений
        is_close = any(w in text for w in ["закрый", "закрой", "выключи", "останови", "убери", "выруби"])
        
        if is_close:
            if is_close_browser_text(text):
                if close_browser():
                    context.speak("Закрываю.")
                else:
                    context.speak("Уже закрыт.")
                return

            if "системный монитор" in text:
                subprocess.Popen(["pkill", "-f", "gnome-system-monitor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Закрываю.")
                return
                
            if any(w in text for w in ["шахматы", "chess"]):
                subprocess.Popen(["pkill", "-f", "gnome-chess"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.Popen(["pkill", "-f", "chrome.*lichess.org"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Закрываю.")
                return

        if "системный монитор" in text:
            context.speak("Открываю.")
            subprocess.Popen(["gnome-system-monitor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if any(w in text for w in ["шахматы", "chess"]):
            if shutil.which("gnome-chess"):
                context.speak("Запускаю.")
                subprocess.Popen(["gnome-chess"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                context.speak("Открываю.")
                subprocess.Popen(
                    chrome_command() + ["--app=https://lichess.org"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return

        # 4. Системные и стандартные приложения
        if "терминал" in text:
            context.speak("Открываю.")
            subprocess.Popen(["gnome-terminal"])
            return

        if "файлы" in text or "проводник" in text:
            context.speak("Открываю.")
            subprocess.Popen(["nautilus"])
            return

        if any(w in text for w in ("системные настройки", "настройки системы", "параметры системы")) or (
            "настройки" in text and any(w in text for w in ("системн", "gnome", "сети", "экрана", "звука"))
        ):
            context.speak("Открываю.")
            subprocess.Popen(["gnome-control-center"])
            return

        if "настройки" in text and not any(
            w in text for w in ("ассистента", "джарвиса", "окно настроек")
        ):
            context.speak("Открываю.")
            subprocess.Popen(["gnome-control-center"])
            return

        if "громче" in text or "громкость плюс" in text:
            subprocess.Popen(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "15%+"])
            context.speak(random.choice(SUCCESS_RESPONSES))
            return

        if "тише" in text or "громкость минус" in text:
            subprocess.Popen(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "20%-"])
            context.speak(random.choice(SUCCESS_RESPONSES))
            return

        if any(w in text for w in ["выключ", "отключ"]) and any(w in text for w in ["компьютер", "пк"]):
            context.speak("Выключаю.")
            time.sleep(1)
            subprocess.Popen(["shutdown", "now"])
            return
