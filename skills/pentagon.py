import subprocess
import logging
from skills.base import BaseSkill, RequestContext

logger = logging.getLogger(__name__)


class PentagonSkill(BaseSkill):
    """Шуточный навык для запуска визуализации в стиле Матрицы."""

    def can_handle(self, context: RequestContext) -> bool:
        lowered = context.raw_text.lower().strip()
        # Шутка про взлом Пентагона / запуск терминала с зелёным кодом матрицы
        if any(w in lowered for w in ["пентагон", "пентагона"]):
            return True
        matrix_triggers = ["взлом матрицы", "взломай матрицу", "эффект матрицы", "режим матрицы", "запусти матрицу", "включи матрицу", "код матрицы"]
        if any(w in lowered for w in matrix_triggers):
            return True
        if lowered in ["матрица", "матрицу"]:
            return True
        return False

    def execute(self, context: RequestContext) -> None:
        try:
            subprocess.Popen([
                "gnome-terminal",
                "--full-screen",
                "--", "bash", "-c", "sleep 0.2 && cmatrix -b -s -C green",
            ])
            context.speak("Взлом Пентагона запущен. Получаю доступ к секретным файлам.")
        except Exception as exc:
            logger.error(f"[PentagonSkill] Ошибка запуска: {exc}")
            context.speak("Не удалось запустить протокол взлома.")
