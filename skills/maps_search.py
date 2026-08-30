import os
import urllib.parse
import subprocess
import logging
from skills.base import BaseSkill, RequestContext

# Словарь для конвертации числительных (включая основные падежи и порядковые формы)
RU_NUMS = {
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1, "одно": 1, "первый": 1, "первая": 1, "первое": 1, "первого": 1, "первую": 1, "одну": 1,
    "два": 2, "две": 2, "второй": 2, "вторая": 2, "второе": 2, "второго": 2, "вторую": 2, "двух": 2,
    "три": 3, "третий": 3, "третья": 3, "третье": 3, "третьего": 3, "трех": 3,
    "четыре": 4, "четвертый": 4, "четвертая": 4, "четвертого": 4, "четырех": 4,
    "пять": 5, "пятый": 5, "пятая": 5, "пятого": 5, "пяти": 5,
    "шесть": 6, "шестой": 6, "шестого": 6, "шести": 6,
    "семь": 7, "седьмой": 7, "седьмого": 7, "семи": 7,
    "восемь": 8, "восьмой": 8, "восьмого": 8, "восьми": 8,
    "девять": 9, "девятый": 9, "девятого": 9, "девяти": 9,
    "десять": 10, "десятый": 10, "десятого": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцатый": 11, "одиннадцатого": 11,
    "двенадцать": 12, "двенадцатый": 12, "двенадцатого": 12,
    "тринадцать": 13, "тринадцатый": 13,
    "четырнадцать": 14, "четырнадцатый": 14,
    "пятнадцать": 15, "пятнадцатый": 15,
    "шестнадцать": 16, "шестнадцатый": 16,
    "семнадцать": 17, "семнадцатый": 17,
    "восемнадцать": 18, "восемнадцатый": 18,
    "девятнадцать": 19, "девятнадцатый": 19,
    "двадцать": 20, "двадцатый": 20, "двадцатого": 20,
    "тридцать": 30, "тридцатый": 30,
    "сорок": 40, "сороковой": 40,
    "пятьдесят": 50, "пятидесятый": 50,
    "шестьдесят": 60, "шестидесятый": 60,
    "семьдесят": 70, "семидесятый": 70,
    "восемьдесят": 80, "восьмидесятый": 80,
    "девяносто": 90, "девяностый": 90,
    "сто": 100, "сотый": 100,
    "двести": 200, "двухсотый": 200,
    "триста": 300, "трехсотый": 300,
    "четыреста": 400, "четырехсотый": 400,
    "пятьсот": 500, "пятисотый": 500,
    "шестьсот": 600, "шестисотый": 600,
    "семьсот": 700, "семисотый": 700,
    "восемьсот": 800, "восьмисотый": 800,
    "девятьсот": 900, "девятисотый": 900,
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячный": 1000
}

# Триггеры для обычного поиска мест/адресов
SEARCH_TRIGGERS = [
    "где находится", 
    "найди на карте", 
    "покажи на карте", 
    "где расположена", 
    "где расположен",
    "где на карте"
]

# Триггеры для автоматического построения маршрутов
ROUTE_TRIGGERS = [
    "как проехать до",
    "как доехать до",
    "как добраться до",
    "маршрут до",
    "построй маршрут до",
    "как пройти до",
    "как дойти до"
]

class MapsSearchSkill(BaseSkill):
    """Навык для поиска географических объектов и построения маршрутов на Яндекс или Google Картах."""

    def __init__(self):
        self.provider = os.getenv("MAPS_PROVIDER", "yandex").lower().strip()
        if self.provider not in ["yandex", "google"]:
            self.provider = "yandex"

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        all_triggers = SEARCH_TRIGGERS + ROUTE_TRIGGERS
        return any(trigger in text for trigger in all_triggers)

    def _words_to_digits(self, text: str) -> str:
        """Преобразует текстовые числительные на русском языке в цифры."""
        words = text.split()
        new_words = []
        
        current_number = 0
        in_number = False
        
        for word in words:
            # Очищаем слово от знаков препинания для корректного поиска в словаре
            cleaned = word.strip(".,!?()\"';:-").lower()
            
            if cleaned in RU_NUMS:
                val = RU_NUMS[cleaned]
                if val == 1000:
                    if current_number == 0:
                        current_number = 1000
                    else:
                        current_number *= 1000
                else:
                    current_number += val
                in_number = True
            else:
                if in_number:
                    new_words.append(str(current_number))
                    current_number = 0
                    in_number = False
                new_words.append(word)
                
        if in_number:
            new_words.append(str(current_number))
            
        return " ".join(new_words)

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        
        used_trigger = None
        is_route = False
        
        # Сначала проверяем маршруты (высокий приоритет)
        for trigger in ROUTE_TRIGGERS:
            if trigger in text:
                used_trigger = trigger
                is_route = True
                break
                
        if not used_trigger:
            for trigger in SEARCH_TRIGGERS:
                if trigger in text:
                    used_trigger = trigger
                    break
                    
        if used_trigger:
            # Оставляем только название точки назначения
            query = text.split(used_trigger, 1)[-1].strip()
        else:
            query = text

        if not query:
            context.speak("Какое именно место вы хотите найти?")
            return

        # Преобразуем текстовые числительные в цифры (например, "десять" -> "10")
        query = self._words_to_digits(query)

        encoded_query = urllib.parse.quote_plus(query)

        # 1. Сценарий: Построение маршрута
        if is_route:
            if any(w in text for w in ["пройти", "дойти", "пешком"]):
                transport_type = "pedestrian"
                context.speak(f"Строю пеший маршрут до: {query}")
            elif any(w in text for w in ["автобус", "метро", "трамвай", "транспорт", "троллейбус"]):
                transport_type = "transit"
                context.speak(f"Строю маршрут на общественном транспорте до: {query}")
            elif any(w in text for w in ["велосипед", "самокат"]):
                transport_type = "bicycle"
                context.speak(f"Строю велосипедный маршрут до: {query}")
            else:
                transport_type = "driving"
                context.speak(f"Строю автомобильный маршрут до: {query}")

            if self.provider == "yandex":
                rtt_map = {"driving": "auto", "pedestrian": "pd", "transit": "mt", "bicycle": "bc"}
                rtt = rtt_map.get(transport_type, "auto")
                maps_url = f"https://yandex.ru/maps/?rtext=~{encoded_query}&rtt={rtt}"
            else:
                mode_map = {"driving": "driving", "pedestrian": "walking", "transit": "transit", "bicycle": "bicycling"}
                travelmode = mode_map.get(transport_type, "driving")
                maps_url = f"https://www.google.com/maps/dir/?api=1&destination={encoded_query}&travelmode={travelmode}"

        # 2. Сценарий: Обычный поиск объектов
        else:
            if self.provider == "yandex":
                context.speak(f"Ищу на Яндекс Картах: {query}")
                maps_url = f"https://yandex.ru/maps/?text={encoded_query}"
            else:
                context.speak(f"Ищу на Google Картах: {query}")
                maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_query}"

        self._open_in_browser(maps_url)

    def _open_in_browser(self, url: str) -> None:
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
                logging.error(f"[MapsSearch] Не удалось открыть карту: {e}")
