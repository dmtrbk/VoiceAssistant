# skills/datetime_skill.py

from datetime import datetime
from skills.base import BaseSkill, RequestContext

class DateTimeSkill(BaseSkill):
    """Навык для озвучивания текущего времени или даты."""

    def can_handle(self, context: RequestContext) -> bool:
        # Проверяем, есть ли триггерные слова в распознанном тексте
        text = context.raw_text
        triggers = [
            "сколько время", "который час", "какое сегодня число", 
            "какой день", "какое число", "текущее время"
        ]
        return any(w in text for w in triggers)

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text
        now = datetime.now()

        # Если запрос касается даты
        if any(w in text for w in ["число", "день", "дата"]):
            months = [
                "января", "февраля", "марта", "апреля", "мая", "июня",
                "июля", "августа", "сентября", "октября", "ноября", "декабря"
            ]
            day = now.day
            month = months[now.month - 1]
            year = now.year
            
            response = f"Сегодня {day} {month} {year} года."
        
        # Если запрос касается времени
        else:
            hours = now.hour
            minutes = now.minute
            response = f"Сейчас {hours} ч., {minutes} мин."

        # Озвучиваем результат пользователю
        context.speak(response)
