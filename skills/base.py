# skills/base.py

from dataclasses import dataclass, field
from typing import Callable, Any, Dict

@dataclass
class RequestContext:
    """Контекст запроса, передаваемый навыкам на обработку."""
    raw_text: str                          # Исходный очищенный текст команды
    intent: str = ""                       # Имя намерения, определенное NLU
    confidence: float = 0.0                # Уверенность классификатора NLU (от 0.0 до 1.0)
    slots: Dict[str, Any] = field(default_factory=dict) # Извлеченные сущности (параметры)
    speak: Callable[[str], None] = None    # Функция для воспроизведения голоса

class BaseSkill:
    """Базовый абстрактный класс для всех навыков ассистента."""
    
    def can_handle(self, context: RequestContext) -> bool:
        """Определяет, может ли данный навык обработать запрос."""
        raise NotImplementedError("Каждый навык должен реализовывать метод can_handle.")

    def execute(self, context: RequestContext) -> None:
        """Выполняет логику навыка."""
        raise NotImplementedError("Каждый навык должен реализовывать метод execute.")
