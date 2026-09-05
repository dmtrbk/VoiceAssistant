# skills/weather.py

import os
import re
import logging
import requests
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)

# Расшифровка WMO кодов погоды на естественный русский язык
WMO_WEATHER_CODES = {
    0: "ясно",
    1: "в основном ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь и туман",
    51: "слабая морось",
    53: "умеренная морось",
    55: "плотная морось",
    56: "ледяная морось",
    57: "густая ледяная морось",
    61: "небольшой дождь",
    63: "умеренный дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снегопад",
    73: "умеренный снегопад",
    75: "сильный снегопад",
    77: "снежные зерна",
    80: "кратковременный дождь",
    81: "умеренный ливень",
    82: "сильный ливень",
    85: "кратковременный снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "гроза с сильным градом",
}

CITY_ALIASES = {
    "питер": "Санкт-Петербург",
    "питере": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "мск": "Москва",
    "москве": "Москва",
    "екб": "Екатеринбург",
    "екатеринбурге": "Екатеринбург",
    "сочи": "Сочи",
    "казани": "Казань",
    "казань": "Казань",
    "краснодаре": "Краснодар",
    "новосибирске": "Новосибирск",
    "самаре": "Самара",
    "уфе": "Уфа",
}


def format_temperature(temp_val: float) -> str:
    """Форматирует температуру с правильным знаком и склонением слова 'градус'."""
    temp_int = round(temp_val)
    if temp_int > 0:
        sign = "плюс "
    elif temp_int < 0:
        sign = "минус "
    else:
        return "0 градусов"

    abs_temp = abs(temp_int)
    last_digit = abs_temp % 10
    last_two = abs_temp % 100

    if 11 <= last_two <= 14:
        word = "градусов"
    elif last_digit == 1:
        word = "градус"
    elif 2 <= last_digit <= 4:
        word = "градуса"
    else:
        word = "градусов"

    return f"{sign}{abs_temp} {word}"


def format_wind(speed_kmh: float) -> str:
    """Конвертирует скорость ветра из км/ч в м/с и возвращает текст."""
    speed_ms = round(speed_kmh / 3.6, 1)
    return f"{speed_ms:g} метра в секунду" if speed_ms in [2, 3, 4] else f"{speed_ms:g} метров в секунду"


class WeatherSkill(BaseSkill):
    """Фирменный навык прогноза погоды в стиле Алисы."""

    def __init__(self):
        self.default_city = os.getenv("DEFAULT_CITY", "Москва").strip()
        self._cached_coords = {}

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        weather_triggers = [
            "погода", "погоду", "погоде", "прогноз погоды",
            "температура", "температуру", "градусов на улице",
            "сколько градусов", "какая температура", "тепло на улице",
            "холодно на улице", "будет ли дождь", "будет дождь",
            "пойдет ли дождь", "пойдет дождь", "будет ли снег",
            "пойдет ли снег", "зонт нужен", "брать ли зонт",
            "брать зонт", "осадки", "давление", "ветер на улице"
        ]
        return any(trigger in text for trigger in weather_triggers)

    def _extract_target_city(self, text: str) -> tuple[str, bool]:
        """Извлекает название города и признак запроса на завтра."""
        is_tomorrow = any(w in text for w in ["завтра", "на завтра", "завтрашний", "завтрашняя"])

        # Проверяем поиск по предлогам "в", "во", "по"
        match = re.search(r"\b(?:в|во|по|городе|город)\s+([а-яёА-ЯЁ\-]+(?:\s+[а-яёА-ЯЁ\-]+)?)", text)
        if match:
            raw_city = match.group(1).strip()
            # Отсекаем временные маркеры, если они попали
            raw_city = re.sub(r"\b(городе|город|сегодня|завтра|сейчас|утром|вечером|днем|ночью|пожалуйста)\b", "", raw_city).strip()
            if raw_city:
                alias = CITY_ALIASES.get(raw_city.lower())
                if alias:
                    return alias, is_tomorrow
                # Снимаем падеж, если заканчивается на 'е' или 'и'
                if raw_city.endswith("е") or raw_city.endswith("и") or raw_city.endswith("у"):
                    return raw_city[:-1], is_tomorrow
                return raw_city, is_tomorrow

        # Проверяем алиасы в тексте
        for k, v in CITY_ALIASES.items():
            if re.search(rf"\b{k}\b", text):
                return v, is_tomorrow

        return self.default_city, is_tomorrow

    def _get_coordinates(self, city_name: str) -> tuple[float, float, str] | None:
        """Получает координаты города через Open-Meteo Geocoding API."""
        if city_name in self._cached_coords:
            return self._cached_coords[city_name]

        try:
            url = "https://geocoding-api.open-meteo.com/v1/search"
            params = {"name": city_name, "count": 1, "language": "ru", "format": "json"}
            resp = requests.get(url, params=params, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results")
                if results and len(results) > 0:
                    lat = results[0]["latitude"]
                    lon = results[0]["longitude"]
                    official_name = results[0].get("name", city_name)
                    self._cached_coords[city_name] = (lat, lon, official_name)
                    return lat, lon, official_name
        except Exception as e:
            logger.error(f"[Погода] Ошибка геокодинга для '{city_name}': {e}")

        return None

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        city_query, is_tomorrow = self._extract_target_city(text)

        geo = self._get_coordinates(city_query)
        if not geo:
            # Попробуем fallback на дефолтный город
            geo = self._get_coordinates(self.default_city)
            if not geo:
                context.speak(f"Не удалось найти информацию о погоде для города {city_query}.")
                return

        lat, lon, city_display_name = geo

        try:
            weather_url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "auto"
            }
            resp = requests.get(weather_url, params=params, timeout=5)
            if resp.status_code != 200:
                context.speak("Не удалось получить данные о погоде. Попробуйте позже.")
                return

            data = resp.json()

            # Вопрос про дождь/зонт
            is_rain_query = any(w in text for w in ["дождь", "зонт", "осадки", "дождливо"])

            if is_tomorrow:
                daily = data.get("daily", {})
                codes = daily.get("weather_code", [])
                max_temps = daily.get("temperature_2m_max", [])
                min_temps = daily.get("temperature_2m_min", [])
                precip = daily.get("precipitation_sum", [])

                if len(codes) >= 2:
                    tom_code = codes[1]
                    tom_max = max_temps[1]
                    tom_min = min_temps[1]
                    tom_precip = precip[1] if len(precip) > 1 else 0.0

                    desc = WMO_WEATHER_CODES.get(tom_code, "переменная облачность")
                    temp_desc = format_temperature(tom_max)

                    if is_rain_query:
                        if tom_precip > 0.5 or tom_code in [51, 53, 55, 61, 63, 65, 80, 81, 82, 95]:
                            context.speak(f"Завтра в городе {city_display_name} ожидается дождь. Зонт пригодится! Днем {temp_desc}.")
                        else:
                            context.speak(f"Завтра в городе {city_display_name} без осадков, {desc}. Днем около {temp_desc}.")
                        return

                    context.speak(f"Завтра в городе {city_display_name} {desc}, днем до {temp_desc}, ночью около {format_temperature(tom_min)}.")
                    return

            # Текущая погода
            current = data.get("current", {})
            cur_temp = current.get("temperature_2m", 0.0)
            app_temp = current.get("apparent_temperature", cur_temp)
            w_code = current.get("weather_code", 0)
            wind_speed = current.get("wind_speed_10m", 0.0)
            precipitation = current.get("precipitation", 0.0)

            desc = WMO_WEATHER_CODES.get(w_code, "ясно")
            temp_str = format_temperature(cur_temp)
            app_str = format_temperature(app_temp)
            wind_str = format_wind(wind_speed)

            if is_rain_query:
                if precipitation > 0.1 or w_code in [51, 53, 55, 61, 63, 65, 80, 81, 82, 95]:
                    context.speak(f"Сейчас в городе {city_display_name} идет дождь. Температура {temp_str}.")
                else:
                    context.speak(f"Сейчас в городе {city_display_name} дождя нет, {desc}. Температура {temp_str}.")
                return

            # Полная реплика в стиле Алисы
            speech = f"В городе {city_display_name} сейчас {temp_str}, {desc}. Ощущается как {app_str}. Ветер {wind_str}."
            context.speak(speech)

        except Exception as e:
            logger.error(f"[Погода] Ошибка обработки запроса: {e}")
            context.speak("Не удалось связаться со службой погоды. Попробуйте еще раз.")
