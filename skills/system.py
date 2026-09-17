# skills/system.py

import random
import logging
import re
import subprocess
import shutil
import time
from skills.base import BaseSkill, RequestContext
from skills.security import set_display_power
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

_DISPLAY_NOUN = r"(?:экран(?:а|у|ом|е|ы)?|монитор(?:а|у|ом|е|ы)?|диспле(?:й|я|ю|ем|е))"
_DISPLAY_OFF_VERB = r"(?:выключи|выключить|погаси|погасить|отключи|отключить|выруби|вырубить|гаси)"
_DISPLAY_ON_VERB = r"(?:включи|включить)"
_DISPLAY_OFF_RE = re.compile(
    rf"(?:{_DISPLAY_OFF_VERB}(?:\s+пожалуйста)?\s+{_DISPLAY_NOUN}"
    rf"|{_DISPLAY_NOUN}\s+{_DISPLAY_OFF_VERB})"
)
_DISPLAY_ON_RE = re.compile(
    rf"(?:{_DISPLAY_ON_VERB}(?:\s+пожалуйста)?\s+{_DISPLAY_NOUN}"
    rf"|{_DISPLAY_NOUN}\s+{_DISPLAY_ON_VERB})"
)


def detect_display_power_action(text: str) -> str | None:
    """off | on | None. «Выключи экран» — монитор, не охрана и не системный монитор."""
    lowered = re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е").strip())
    if not lowered:
        return None
    if "системный монитор" in lowered:
        return None
    if "на весь экран" in lowered or "полный экран" in lowered:
        return None
    if "настройк" in lowered:
        return None
    if _DISPLAY_OFF_RE.search(lowered):
        return "off"
    if _DISPLAY_ON_RE.search(lowered):
        return "on"
    return None


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
            or detect_display_power_action(text) is not None
            or is_close_browser_text(text)
            or detect_window_action(text) is not None
        )

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        display_action = detect_display_power_action(text)
        if display_action == "off":
            context.speak("Выключаю.")
            time.sleep(2)
            if set_display_power(False):
                logging.info("[Система] Экран выключен.")
                from skills.ai_chat import log_system_action
                log_system_action("Пользователь выключил экран")
            else:
                context.speak("Не вышло.")
            return

        if display_action == "on":
            if set_display_power(True):
                logging.info("[Система] Экран включен.")
                from skills.ai_chat import log_system_action
                log_system_action("Пользователь включил экран")
                context.speak("Включил.")
            else:
                context.speak("Не вышло.")
            return

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
