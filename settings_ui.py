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

from skill_settings import (
    OPTIONAL_SKILLS,
    get_flags,
    is_auto_trade_enabled,
    set_auto_trade,
    set_flag,
)


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
            QFrame#submenu { padding: 6px 0 2px 22px; }
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
        self._auto_trade_box: QCheckBox | None = None
        self._auto_trade_menu: QFrame | None = None
        self._rebuild()

    def showEvent(self, event):
        self._rebuild()
        super().showEvent(event)

    def _rebuild(self) -> None:
        flags = get_flags()
        expected = [item[0] for item in OPTIONAL_SKILLS]
        if self._boxes and list(self._boxes.keys()) == expected and self._auto_trade_box is not None:
            for skill_id, box in self._boxes.items():
                box.blockSignals(True)
                box.setChecked(flags.get(skill_id, True))
                box.blockSignals(False)
            self._sync_auto_trade_menu(flags.get("stocks", True))
            return

        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._boxes.clear()
        self._auto_trade_box = None
        self._auto_trade_menu = None

        for skill_id, title, hint in OPTIONAL_SKILLS:
            row = QFrame()
            row.setObjectName("row")
            col = QVBoxLayout(row)
            col.setContentsMargins(0, 6, 0, 8)
            col.setSpacing(2)

            box = QCheckBox(title)
            box.setChecked(flags.get(skill_id, True))
            box.toggled.connect(lambda checked, sid=skill_id: set_flag(sid, checked))
            if skill_id == "stocks":
                box.toggled.connect(self._on_stocks_toggled)
            self._boxes[skill_id] = box
            col.addWidget(box)

            caption = QLabel(hint)
            caption.setObjectName("hint")
            caption.setWordWrap(True)
            col.addWidget(caption)
            if skill_id == "stocks":
                self._add_auto_trade_menu(col, flags.get("stocks", True))
            self._list.addWidget(row)

        self._list.addStretch(1)

    def _add_auto_trade_menu(self, parent: QVBoxLayout, stocks_on: bool) -> None:
        menu = QFrame()
        menu.setObjectName("submenu")
        col = QVBoxLayout(menu)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(2)

        box = QCheckBox("Автоторговля")
        box.setChecked(is_auto_trade_enabled())
        box.toggled.connect(set_auto_trade)
        col.addWidget(box)

        caption = QLabel(
            "Фон сам ставит заявки примерно раз в 45 минут. На живом счёте — реальные сделки."
        )
        caption.setObjectName("hint")
        caption.setWordWrap(True)
        col.addWidget(caption)

        self._auto_trade_box = box
        self._auto_trade_menu = menu
        parent.addWidget(menu)
        self._sync_auto_trade_menu(stocks_on)

    def _sync_auto_trade_menu(self, stocks_on: bool) -> None:
        if self._auto_trade_box is None or self._auto_trade_menu is None:
            return
        self._auto_trade_box.blockSignals(True)
        self._auto_trade_box.setChecked(is_auto_trade_enabled())
        self._auto_trade_box.setEnabled(stocks_on)
        self._auto_trade_box.blockSignals(False)
        self._auto_trade_menu.setVisible(stocks_on)

    def _on_stocks_toggled(self, checked: bool) -> None:
        self._sync_auto_trade_menu(checked)
