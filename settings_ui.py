# settings_ui.py
# Обычное окно Qt: только необязательные навыки. Каркас скрыт.

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from skill_settings import OPTIONAL_SKILLS, get_flags, set_flag


class SettingsWindow(QWidget):
    """Список тумблеров. Изменение сразу пишется на диск, служба не перезапускается."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Настройки Джарвиса")
        self.setMinimumWidth(380)
        self.setMinimumHeight(420)
        self.resize(400, 520)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(
            """
            QWidget { background: #2d3339; color: #ecf0f1; font-size: 14px; }
            QLabel#title { font-size: 18px; font-weight: 600; color: #35BF5C; }
            QLabel#hint { color: #95a5a6; font-size: 12px; }
            QCheckBox { spacing: 10px; padding: 4px 0; }
            QCheckBox::indicator { width: 20px; height: 20px; }
            QFrame#row { border-bottom: 1px solid #3d444b; padding: 6px 0; }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Навыки")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel("Выключенный навык не отвечает на голос и в Telegram.")
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self._list = QVBoxLayout(body)
        self._list.setContentsMargins(0, 8, 8, 8)
        self._list.setSpacing(2)
        scroll.setWidget(body)
        root.addWidget(scroll)

        self._boxes: dict[str, QCheckBox] = {}
        self._rebuild()

    def showEvent(self, event):
        self._rebuild()
        super().showEvent(event)

    def _rebuild(self) -> None:
        flags = get_flags()
        if self._boxes:
            for skill_id, box in self._boxes.items():
                box.blockSignals(True)
                box.setChecked(flags.get(skill_id, True))
                box.blockSignals(False)
            return

        for skill_id, title, hint in OPTIONAL_SKILLS:
            row = QFrame()
            row.setObjectName("row")
            col = QVBoxLayout(row)
            col.setContentsMargins(0, 6, 0, 8)
            col.setSpacing(2)

            box = QCheckBox(title)
            box.setChecked(flags.get(skill_id, True))
            box.toggled.connect(lambda checked, sid=skill_id: set_flag(sid, checked))
            self._boxes[skill_id] = box
            col.addWidget(box)

            caption = QLabel(hint)
            caption.setObjectName("hint")
            caption.setWordWrap(True)
            col.addWidget(caption)
            self._list.addWidget(row)

        self._list.addStretch(1)
