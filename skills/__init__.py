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
from .restart import RestartSkill       # <-- 1. Импортируем навык перезагрузки

# Создаем экземпляры навыков
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
restart_skill = RestartSkill()         # <-- 2. Инициализируем навык перезагрузки

# Очередь приоритетов опроса навыков.
ALL_SKILLS = [
    local_nlu_skill,
    restart_skill,                     # <-- 3. Помещаем в начало приоритета
    security_skill,
    datetime_skill,
    xiaomi_bulb_skill,
    audacious_skill,                         
    web_search_skill,                        
    maps_search_skill,  
    telegram_skill,  
    system_skill,                            
    ai_chat_skill
]
