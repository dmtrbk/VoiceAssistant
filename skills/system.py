# skills/system.py

import os
import re
import random
import logging
import subprocess
import shutil
import time
import wikipediaapi
from skills.base import BaseSkill, RequestContext

SUCCESS_RESPONSES = [
    "Сделано!", "Готово!", "Выполнил.", "Есть!", 
    "Без проблем.", "Сделано, босс!", "Готово, проверяйте."
]

class SystemSkill(BaseSkill):
    """Навык для управления операционной системой Linux (громкость, утилиты, выключение)."""

    def __init__(self):
        self._shutdown_pending_until = 0.0

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()

        # Исключаем вопросы о личности ("кто ты", "кто ты такой", "ты кто"), чтобы они шли в NLU/диалог
        is_identity = any(phrase in text for phrase in ["кто ты", "кто вы", "ты кто", "как тебя зовут"])
        has_wiki = False if is_identity else any(w in text for w in ["википедия", "что такое", "кто такой", "кто такая"])

        triggers = [
            "терминал", "файлы", "проводник", "настройки",
            "громче", "громкость плюс", "тише", "громкость минус",
            "ютуб", "youtube",
            
            # Приложения и утилиты
            "htop", "неофетч", "neofetch", "системный монитор", 
            "тик ток", "тиктоку", "tiktok", "шахматы", "chess"
        ]
        is_shutdown = any(w in text for w in ["выключ", "отключ"]) and any(w in text for w in ["компьютер", "пк"])
        return has_wiki or any(w in text for w in triggers) or is_shutdown

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()

        # 1. Поиск по Википедии
        if any(w in text for w in ["википедия", "что такое", "кто такой", "кто такая"]):
            trigger = next(w for w in ["википедия", "что такое", "кто такой", "кто такая"] if w in text)
            wiki_query = text.split(trigger, 1)[-1].strip()
            if wiki_query:
                context.speak(f"Ищу {wiki_query}")
                try:
                    wiki = wikipediaapi.Wikipedia(user_agent="VoiceAssistantBot/1.0", language="ru")
                    page = wiki.page(wiki_query)
                    if page.exists():
                        sentences = re.split(r'(?<=[.!?])\s+', page.summary)
                        context.speak(" ".join(sentences[:2]))
                    else:
                        context.speak(f"Статья про {wiki_query} не найдена.")
                except Exception as e:
                    logging.error(f"Ошибка Вики: {e}")
            else:
                context.speak("Что именно найти?")
            return

        # 2. Обработка команд ЗАКРЫТИЯ/ОСТАНОВКИ приложений
        is_close = any(w in text for w in ["закрый", "закрой", "выключи", "останови", "убери", "выруби"])
        
        if is_close:
            if "htop" in text:
                subprocess.Popen(["pkill", "-f", "htop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Закрываю htop.")
                return
            
            if any(w in text for w in ["neofetch", "неофетч"]):
                subprocess.Popen(["pkill", "-f", "neofetch"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Убираю neofetch.")
                return
                
            if "системный монитор" in text:
                subprocess.Popen(["pkill", "-f", "gnome-system-monitor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Закрываю системный монитор.")
                return
                
            if any(w in text for w in ["шахматы", "chess"]):
                subprocess.Popen(["pkill", "-f", "gnome-chess"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.Popen(["pkill", "-f", "chrome.*lichess.org"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                context.speak("Закрываю шахматы.")
                return

        # 3. Обработка команд ЗАПУСКА приложений и утилит
        if "htop" in text:
            context.speak("Запускаю htop.")
            subprocess.Popen(["gnome-terminal", "--", "htop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if any(w in text for w in ["neofetch", "неофетч"]):
            context.speak("Запускаю neofetch.")
            subprocess.Popen([
                "gnome-terminal", "--", "bash", "-c", 
                "neofetch; echo; read -n 1 -s -r -p 'Нажмите любую клавишу для закрытия...';"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if "системный монитор" in text:
            context.speak("Открываю системный монитор.")
            subprocess.Popen(["gnome-system-monitor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if any(w in text for w in ["тик ток", "тиктоку", "tiktok"]):
            context.speak("Открываю Тик Ток.")
            subprocess.Popen([
                "/opt/google/chrome/google-chrome", 
                "--profile-directory=Default", 
                "--app-id=nlalbmkafgmoifbeooblidblkmlhhpnc"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if any(w in text for w in ["ютуб", "youtube"]):
            context.speak("Включаю Ютуб.")
            subprocess.Popen([
                "/opt/google/chrome/google-chrome", 
                "--profile-directory=Default", 
                "--app-id=agimnkijcaahngcdmfeangaknmldooml"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if any(w in text for w in ["шахматы", "chess"]):
            if shutil.which("gnome-chess"):
                context.speak("Запускаю шахматы.")
                subprocess.Popen(["gnome-chess"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                context.speak("Локальное приложение не найдено. Открываю шахматный сайт Lichess.")
                subprocess.Popen(["google-chrome-stable", "--app=https://lichess.org"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        # 4. Системные и стандартные приложения
        if "терминал" in text:
            context.speak("Открываю терминал")
            subprocess.Popen(["gnome-terminal"])
            return

        if "файлы" in text or "проводник" in text:
            context.speak("Открываю проводник")
            subprocess.Popen(["nautilus"])
            return

        if "настройки" in text:
            context.speak("Открываю параметры")
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
            now = time.time()
            if now < self._shutdown_pending_until:
                self._shutdown_pending_until = 0.0
                context.speak("Выключаю компьютер. До свидания!")
                time.sleep(1)
                subprocess.Popen(["shutdown", "now"])
            else:
                self._shutdown_pending_until = now + 20
                context.speak("Чтобы выключить компьютер, повторите команду.")
            return
