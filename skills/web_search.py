# skills/web_search.py

import os
import urllib.parse
import subprocess
import logging
from skills.base import BaseSkill, RequestContext

class WebSearchSkill(BaseSkill):
    """Навык для поиска информации в интернете и управления браузером."""

    def __init__(self):
        # Поисковик по умолчанию: "yandex" или "google"
        self.search_provider = os.getenv("SEARCH_PROVIDER", "yandex").lower().strip()
        if self.search_provider not in ["yandex", "google"]:
            self.search_provider = "yandex"

        # Нейросеть по умолчанию: "yandex" (Алиса) или "gemini"
        self.ai_provider = os.getenv("AI_PROVIDER", "yandex").lower().strip()
        if self.ai_provider not in ["yandex", "gemini"]:
            self.ai_provider = "yandex"

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        
        # Триггеры для поиска конкретного запроса
        search_triggers = [
            "найди в интернете", "поиск в интернете", "погугли", 
            "ищи в гугле", "найди в яндексе", "найди в гугле", 
            "поищи в интернете", "загугли", "ищи в яндексе", "поищи в яндексе"
        ]
        
        # Триггеры для запуска браузера / сайтов
        browser_triggers = [
            "открой браузер", "запусти браузер", "включи браузер", "браузер",
            "открой яндекс", "запусти яндекс", "включи яндекс",
            "открой гугл", "запусти гугл", "включи гугл"
        ]
        
        # Триггеры для ИИ
        ai_triggers = [
            "включи ии", "открой ии", "запусти ии", "открой нейросеть", "нейросеть",
            "открой алису", "включи алису", "открой джемини", "открой gemini", "включи gemini"
        ]
        
        has_search = any(trigger in text for trigger in search_triggers)
        has_browser = any(trigger in text for trigger in browser_triggers)
        has_ai = any(trigger in text for trigger in ai_triggers)
        
        return has_search or has_browser or has_ai

    def _open_url(self, url: str) -> None:
        """Вспомогательный метод запуска браузера (Google Chrome или системный по умолчанию)."""
        try:
            subprocess.Popen(
                ["google-chrome-stable", url], 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            try:
                subprocess.Popen(
                    ["xdg-open", url], 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                logging.error(f"[WebSearch] Не удалось открыть браузер: {e}")

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        
        # 1. Открытие ИИ-ассистентов
        # Явный запрос на Gemini
        if any(w in text for w in ["джемини", "gemini"]):
            context.speak("Открываю нейросеть Gemini в браузере.")
            self._open_url("https://gemini.google.com")
            return
            
        # Явный запрос на Алису
        if any(w in text for w in ["алису", "алиса"]):
            context.speak("Открываю нейросеть Алиса ИИ в браузере.")
            self._open_url("https://alice.yandex.ru")
            return

        # Общий запрос на ИИ (открываем то, что настроено по умолчанию)
        if any(w in text for w in ["включи ии", "открой ии", "запусти ии", "открой нейросеть", "нейросеть"]):
            if self.ai_provider == "gemini":
                context.speak("Открываю нейросеть Gemini в браузере.")
                self._open_url("https://gemini.google.com")
            else:
                context.speak("Открываю нейросеть Алиса ИИ в браузере.")
                self._open_url("https://alice.yandex.ru")
            return

        # 2. Простое открытие главной страницы поисковиков или браузера
        if any(w in text for w in ["открой яндекс", "запусти яндекс", "включи яндекс"]):
            context.speak("Открываю Яндекс.")
            self._open_url("https://ya.ru")
            return

        if any(w in text for w in ["открой гугл", "запусти гугл", "включи гугл"]):
            context.speak("Открываю Google.")
            self._open_url("https://www.google.com")
            return

        if any(w in text for w in ["открой браузер", "запусти браузер", "включи браузер", "браузер"]):
            # Проверяем, что это не поисковый запрос (например, "найди в браузере...")
            if not any(t in text for t in ["найди", "поиск", "ищи"]):
                context.speak("Открываю браузер.")
                # В качестве домашней страницы открываем выбранный по умолчанию поисковик
                start_url = "https://ya.ru" if self.search_provider == "yandex" else "https://www.google.com"
                self._open_url(start_url)
                return

        # 3. Поиск информации в интернете
        search_triggers = [
            "найди в интернете", "поиск в интернете", "найди в яндексе", 
            "ищи в яндексе", "поищи в яндексе", "найди в гугле", 
            "ищи в гугле", "поищи в интернете", "погугли", "загугли"
        ]
        
        used_trigger = None
        for trigger in search_triggers:
            if trigger in text:
                used_trigger = trigger
                break
                
        if used_trigger:
            query = text.split(used_trigger, 1)[-1].strip()
            
            if query:
                encoded_query = urllib.parse.quote_plus(query)
                
                # Принудительный поиск в Google
                if any(g in used_trigger for g in ["гугл", "гугле", "загугли", "погугли"]):
                    context.speak(f"Ищу в Гугле: {query}")
                    search_url = f"https://www.google.com/search?q={encoded_query}"
                # Принудительный поиск в Яндексе
                elif "яндекс" in used_trigger:
                    context.speak(f"Ищу в Яндексе: {query}")
                    search_url = f"https://ya.ru/search/?text={encoded_query}"
                # Поиск через провайдера по умолчанию
                else:
                    if self.search_provider == "google":
                        context.speak(f"Ищу в Гугле: {query}")
                        search_url = f"https://www.google.com/search?q={encoded_query}"
                    else:
                        context.speak(f"Ищу в Яндексе: {query}")
                        search_url = f"https://ya.ru/search/?text={encoded_query}"
                    
                self._open_url(search_url)
            else:
                context.speak("Что именно вы хотите найти?")
