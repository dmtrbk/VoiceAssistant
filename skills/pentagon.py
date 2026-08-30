import subprocess
import logging
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger("VoiceAssistant")

class PentagonSkill(BaseSkill):
    """Шуточный навык для запуска визуализации в стиле Матрицы."""

    def can_handle(self, ctx: RequestContext) -> bool:
        raw_text = getattr(ctx, 'text', None) or getattr(ctx, 'command', None) or str(ctx)
        lowered = str(raw_text).lower().strip()
        keywords = ["пентагон", "пентагона", "матриц", "матрицу"]
        return any(word in lowered for word in keywords)

    def execute(self, ctx: RequestContext) -> str:
        try:
            subprocess.Popen([
                "gnome-terminal",
                "--full-screen",
                "--", "bash", "-c", "sleep 0.2 && cmatrix -b -s -C green"
            ])
            return "Взлом Пентагона запущен. Получаю доступ к секретным файлам."
        except Exception as e:
            logger.error(f"[PentagonSkill] Ошибка запуска: {e}")
            return "Не удалось запустить протокол взлома."
