# skills/datetime_skill.py

from datetime import datetime
from skills.base import BaseSkill, RequestContext

DAYS_OF_WEEK = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье"
]

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]


def _plural(n: int, form1: str, form2: str, form5: str) -> str:
    abs_n = abs(n)
    last_two = abs_n % 100
    last_one = abs_n % 10
    if 11 <= last_two <= 14:
        return form5
    if last_one == 1:
        return form1
    if 2 <= last_one <= 4:
        return form2
    return form5


def format_russian_time(dt: datetime) -> str:
    h = dt.hour
    m = dt.minute

    h_word = _plural(h, "час", "часа", "часов")
    if m == 0:
        return f"Сейчас ровно {h} {h_word}."

    m_word = _plural(m, "минута", "минуты", "минут")
    return f"Сейчас {h} {h_word} {m} {m_word}."


class DateTimeSkill(BaseSkill):
    """Навык для озвучивания текущего времени или даты в стиле Алисы."""

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text.lower().strip()
        triggers = [
            "сколько время", "сколько времени", "который час", "какое сегодня число",
            "какой день", "какое число", "текущее время", "подскажи время",
            "тоstatное время", "какой день недели", "день недели", "какой месяц",
            "какой год", "сегодняшняя дата", "какая дата"
        ]
        return any(w in text for w in triggers)

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text.lower().strip()
        now = datetime.now()

        # 1. Запрос на день недели
        if any(w in text for w in ["день недели", "какой сегодня день"]):
            weekday = DAYS_OF_WEEK[now.weekday()]
            day = now.day
            month = MONTHS[now.month - 1]
            context.speak(f"Сегодня {weekday}, {day} {month}.")
            return

        # 2. Запрос на дату / число / год / месяц
        if any(w in text for w in ["число", "дата", "месяц", "год", "сегодняшн"]):
            day = now.day
            month = MONTHS[now.month - 1]
            year = now.year
            weekday = DAYS_OF_WEEK[now.weekday()]
            context.speak(f"Сегодня {weekday}, {day} {month} {year} года.")
            return

        # 3. Запрос на время
        context.speak(format_russian_time(now))
