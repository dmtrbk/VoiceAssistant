# skills/base.py

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class RequestContext:
    """Контекст запроса, передаваемый навыкам на обработку."""
    raw_text: str
    intent: str = ""
    confidence: float = 0.0
    slots: Dict[str, Any] = field(default_factory=dict)
    speak: Optional[Callable[[str], None]] = None
    should_sleep: bool = False
    channel: str = "voice"  # "voice" | "telegram" | "cli"


class BaseSkill:
    """Базовый абстрактный класс для всех навыков ассистента."""

    def can_handle(self, context: RequestContext) -> bool:
        raise NotImplementedError("Каждый навык должен реализовывать метод can_handle.")

    def execute(self, context: RequestContext) -> None:
        raise NotImplementedError("Каждый навык должен реализовывать метод execute.")

    def accepts_followup(self, context: RequestContext) -> bool:
        """Короткая реплика вроде «а завтра?» или «ещё» после этого навыка."""
        return False

    def on_context_lost(self) -> None:
        """Вызывается, когда маршрутизатор переключился на другой навык."""
        return

    def on_disabled(self) -> None:
        """Навык выключили в настройках. Остановить фон (камера и т.п.), без озвучки."""
        return

    def on_enabled(self) -> None:
        """Навык включили в настройках. Возобновить фон, если нужно."""
        return
