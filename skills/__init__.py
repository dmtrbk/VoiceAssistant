# skills/__init__.py

from .system import SystemSkill
from .security import SecuritySkill
from .ai_chat import AIChatSkill
from .local_nlu import LocalNLUSkill
from .datetime_skill import DateTimeSkill
from .xiaomi_bulb import XiaomiBulbSkill
from .audacious import AudaciousSkill
from .web_search import WebSearchSkill
from .maps_search import MapsSearchSkill
from .telegram import TelegramSkill
from .restart import RestartSkill
from .pentagon import PentagonSkill
from .weather import WeatherSkill
from .timer import TimerSkill
from .calculator import CalculatorSkill
from .jokes_facts import JokesAndFactsSkill

system_skill = SystemSkill()
security_skill = SecuritySkill()
ai_chat_skill = AIChatSkill()
local_nlu_skill = LocalNLUSkill()
datetime_skill = DateTimeSkill()
xiaomi_bulb_skill = XiaomiBulbSkill()
audacious_skill = AudaciousSkill()
web_search_skill = WebSearchSkill()
maps_search_skill = MapsSearchSkill()
telegram_skill = TelegramSkill()
restart_skill = RestartSkill()
pentagon_skill = PentagonSkill()
weather_skill = WeatherSkill()
timer_skill = TimerSkill()
calculator_skill = CalculatorSkill()
jokes_skill = JokesAndFactsSkill()

# Приоритет навыков: специализированные узкие навыки обрабатываются первыми,
# затем локальный NLU (приветствия, прощания), и в конце fallback — диалоговый ИИ Groq.
ALL_SKILLS = [
    restart_skill,
    pentagon_skill,
    security_skill,
    timer_skill,
    calculator_skill,
    weather_skill,
    datetime_skill,
    jokes_skill,
    xiaomi_bulb_skill,
    audacious_skill,
    web_search_skill,
    maps_search_skill,
    telegram_skill,
    system_skill,
    local_nlu_skill,
    ai_chat_skill,
]
