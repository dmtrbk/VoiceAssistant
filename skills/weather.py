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
    "краснодар": "Краснодар",
    "новосибирске": "Новосибирск",
    "новосибирск": "Новосибирск",
    "самаре": "Самара",
    "самара": "Самара",
    "уфе": "Уфа",
    "уфа": "Уфа",
}

_CITY_FILLERS = (
    "городе", "город", "сегодня", "завтра", "сейчас",
    "утром", "вечером", "днем", "днём", "ночью", "пожалуйста",
)

_FOLLOWUP_MARKERS = (
    "завтра", "сегодня", "послезавтра", "дождь", "зонт",
    "осадки", "там",
)


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
    """Прогноз погоды: город из фразы или точка DEFAULT_LAT/DEFAULT_LON."""

    def __init__(self):
        named_city = (os.getenv("DEFAULT_CITY") or "").strip()
        self._cached_coords: dict[str, tuple[float, float, str]] = {}
        self._last_city: str | None = None
        self._last_was_default = False
        self._default_coords: tuple[float, float] | None = None
        self._default_label = named_city

        default_lat = os.getenv("DEFAULT_LAT") or os.getenv("DEFAULT_LATITUDE")
        default_lon = os.getenv("DEFAULT_LON") or os.getenv("DEFAULT_LONGITUDE")
        if default_lat and default_lon:
            try:
                lat = float(default_lat)
                lon = float(default_lon)
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    raise ValueError("вне диапазона")
                self._default_coords = (lat, lon)
                if not self._default_label:
                    self._default_label = "здесь"
                logger.info(
                    "[Погода] Точка по умолчанию: %s, %s (%s)",
                    lat, lon, self._default_label,
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[Погода] Некорректные координаты в .env: %s, %s (%s)",
                    default_lat, default_lon, exc,
                )

        if not self._default_coords and not self._default_label:
            self._default_label = "Москва"

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

    def accepts_followup(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        if any(marker in text for marker in _FOLLOWUP_MARKERS):
            return True
        for alias in CITY_ALIASES:
            if re.search(rf"\b{re.escape(alias)}\b", text):
                return True
        return False

    def _extract_target_city(self, text: str) -> tuple[str | None, bool]:
        """Город из фразы. None — точка по умолчанию (координаты или DEFAULT_CITY)."""
        is_tomorrow = any(w in text for w in ["завтра", "на завтра", "завтрашний", "завтрашняя"])

        match = re.search(
            r"\b(?:в|во|по|городе|город)\s+([а-яёА-ЯЁ\-]+(?:\s+[а-яёА-ЯЁ\-]+)?)",
            text,
        )
        if match:
            raw_city = match.group(1).strip()
            raw_city = re.sub(
                r"\b(" + "|".join(_CITY_FILLERS) + r")\b",
                "",
                raw_city,
            ).strip()
            if raw_city:
                alias = CITY_ALIASES.get(raw_city.lower())
                if alias:
                    return alias, is_tomorrow
                return raw_city, is_tomorrow

        for key, official in CITY_ALIASES.items():
            if re.search(rf"\b{re.escape(key)}\b", text):
                return official, is_tomorrow

        if self._last_city and not self._last_was_default:
            return self._last_city, is_tomorrow
        return None, is_tomorrow

    def _default_geo(self) -> tuple[float, float, str] | None:
        if self._default_coords is not None:
            lat, lon = self._default_coords
            return lat, lon, self._default_label
        if self._default_label:
            return self._get_coordinates(self._default_label)
        return None

    def _place_clause(self, name: str, is_default: bool) -> str:
        if is_default and name.lower() in {"здесь", "дома", "у нас"}:
            return "Здесь"
        return f"В городе {name}"

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
        used_default = city_query is None

        if used_default:
            geo = self._default_geo()
        else:
            geo = self._get_coordinates(city_query)

        if not geo:
            if used_default:
                context.speak("Не удалось получить погоду для вашей точки. Проверьте координаты в настройках.")
            else:
                context.speak(f"Не удалось найти информацию о погоде для города {city_query}.")
            return

        lat, lon, city_display_name = geo
        self._last_city = None if used_default else city_display_name
        self._last_was_default = used_default
        place = self._place_clause(city_display_name, used_default)

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
                            context.speak(f"Завтра {place.lower()} ожидается дождь. Зонт пригодится! Днем {temp_desc}.")
                        else:
                            context.speak(f"Завтра {place.lower()} без осадков, {desc}. Днем около {temp_desc}.")
                        return

                    context.speak(
                        f"Завтра {place.lower()} {desc}, днем до {temp_desc}, ночью около {format_temperature(tom_min)}."
                    )
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
                    context.speak(f"Сейчас {place.lower()} идет дождь. Температура {temp_str}.")
                else:
                    context.speak(f"Сейчас {place.lower()} дождя нет, {desc}. Температура {temp_str}.")
                return

            speech = (
                f"{place} сейчас {temp_str}, {desc}. "
                f"Ощущается как {app_str}. Ветер {wind_str}."
            )
            context.speak(speech)

        except Exception as e:
            logger.error(f"[Погода] Ошибка обработки запроса: {e}")
            context.speak("Не удалось связаться со службой погоды. Попробуйте еще раз.")
