# settings_ui.py
# Обычное окно Qt: только необязательные навыки. Каркас скрыт.

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from skill_settings import (
    OPTIONAL_SKILLS,
    get_flags,
    get_groq_model,
    is_auto_trade_enabled,
    is_cursor_running,
    is_voice_trade_enabled,
    set_auto_trade,
    set_flag,
    set_groq_model,
    set_voice_trade,
)
from skills.groq_client import FAST_MODEL, groq_model_choices


class SettingsWindow(QWidget):
    """Список тумблеров. Изменение сразу пишется на диск, служба не перезапускается."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Настройки Джарвиса")
        self.setMinimumWidth(380)
        self.setMinimumHeight(460)
        self.resize(400, 560)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(
            """
            QWidget { background: #2d3339; color: #ecf0f1; font-size: 14px; }
            QLabel#title { font-size: 18px; font-weight: 600; color: #35BF5C; }
            QLabel#hint { color: #95a5a6; font-size: 12px; }
            QCheckBox { spacing: 10px; padding: 4px 0; }
            QCheckBox::indicator { width: 20px; height: 20px; }
            QComboBox {
                background: #3d444b; color: #ecf0f1; padding: 4px 8px;
                border: 1px solid #4d555c; border-radius: 4px; min-height: 26px;
            }
            QComboBox::drop-down { border: none; width: 22px; }
            QComboBox QAbstractItemView {
                background: #3d444b; color: #ecf0f1; selection-background-color: #1f7a3a;
            }
            QFrame#row { border-bottom: 1px solid #3d444b; padding: 6px 0; }
            QFrame#submenu { padding: 6px 0 2px 22px; }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        self._model_combo: QComboBox | None = None
        self._model_hint: QLabel | None = None
        self._add_model_row(root)

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
        self._voice_trade_box: QCheckBox | None = None
        self._auto_trade_box: QCheckBox | None = None
        self._stocks_menu: QFrame | None = None
        self._rebuild()

    def showEvent(self, event):
        self._rebuild()
        self._sync_model_row()
        super().showEvent(event)

    def _add_model_row(self, parent: QVBoxLayout) -> None:
        heading = QLabel("Модель диалога")
        heading.setObjectName("title")
        heading.setStyleSheet("font-size: 15px; font-weight: 600; color: #35BF5C;")
        parent.addWidget(heading)

        combo = QComboBox()
        for model_id, title in groq_model_choices():
            combo.addItem(title, model_id)
        combo.currentIndexChanged.connect(self._on_model_changed)
        self._model_combo = combo
        parent.addWidget(combo)

        hint = QLabel()
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self._model_hint = hint
        parent.addWidget(hint)
        self._sync_model_row()

    def _sync_model_row(self) -> None:
        if self._model_combo is None or self._model_hint is None:
            return
        current = get_groq_model()
        known = {self._model_combo.itemData(i) for i in range(self._model_combo.count())}
        if current not in known:
            self._model_combo.addItem(current, current)
        index = self._model_combo.findData(current)
        self._model_combo.blockSignals(True)
        if index >= 0:
            self._model_combo.setCurrentIndex(index)
        self._model_combo.blockSignals(False)

        cursor_on = is_cursor_running()
        strong = current != FAST_MODEL
        if cursor_on and strong:
            self._model_hint.setText(
                "Сильная выбрана. Пока открыт Cursor, отвечает быстрая — квота Groq не угорает. "
                "Закрой редактор, и Джарвис переключится."
            )
        elif cursor_on:
            self._model_hint.setText(
                "Сейчас быстрая. Сильную можно выбрать заранее: заработает, когда закроешь Cursor."
            )
        elif strong:
            self._model_hint.setText(
                "Сильная модель. Если откроешь Cursor, временно вернётся быстрая."
            )
        else:
            self._model_hint.setText(
                "Быстрая модель. Сильную удобнее включать, когда Cursor закрыт."
            )

    def _on_model_changed(self, index: int) -> None:
        if self._model_combo is None or index < 0:
            return
        model_id = self._model_combo.itemData(index)
        if not model_id:
            return
        set_groq_model(str(model_id))
        self._sync_model_row()

    def _rebuild(self) -> None:
        flags = get_flags()
        expected = [item[0] for item in OPTIONAL_SKILLS]
        if (
            self._boxes
            and list(self._boxes.keys()) == expected
            and self._voice_trade_box is not None
            and self._auto_trade_box is not None
        ):
            for skill_id, box in self._boxes.items():
                box.blockSignals(True)
                box.setChecked(flags.get(skill_id, True))
                box.blockSignals(False)
            self._sync_stocks_menu(flags.get("stocks", True))
            return

        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._boxes.clear()
        self._voice_trade_box = None
        self._auto_trade_box = None
        self._stocks_menu = None

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
                self._add_stocks_menu(col, flags.get("stocks", True))
            self._list.addWidget(row)

        self._list.addStretch(1)

    def _add_stocks_menu(self, parent: QVBoxLayout, stocks_on: bool) -> None:
        menu = QFrame()
        menu.setObjectName("submenu")
        col = QVBoxLayout(menu)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(8)

        voice_box = QCheckBox("Сделки голосом")
        voice_box.setChecked(is_voice_trade_enabled())
        voice_box.toggled.connect(set_voice_trade)
        col.addWidget(voice_box)

        voice_hint = QLabel(
            "«Купи», «продай», «поторгуй» выставляют заявки. "
            "Выключено — только сводка, сделки в приложении брокера."
        )
        voice_hint.setObjectName("hint")
        voice_hint.setWordWrap(True)
        col.addWidget(voice_hint)

        auto_box = QCheckBox("Автоторговля")
        auto_box.setChecked(is_auto_trade_enabled())
        auto_box.toggled.connect(set_auto_trade)
        col.addWidget(auto_box)

        auto_hint = QLabel(
            "Фон сам ставит заявки примерно раз в 45 минут. На живом счёте — реальные сделки."
        )
        auto_hint.setObjectName("hint")
        auto_hint.setWordWrap(True)
        col.addWidget(auto_hint)

        self._voice_trade_box = voice_box
        self._auto_trade_box = auto_box
        self._stocks_menu = menu
        parent.addWidget(menu)
        self._sync_stocks_menu(stocks_on)

    def _sync_stocks_menu(self, stocks_on: bool) -> None:
        if (
            self._voice_trade_box is None
            or self._auto_trade_box is None
            or self._stocks_menu is None
        ):
            return
        self._voice_trade_box.blockSignals(True)
        self._voice_trade_box.setChecked(is_voice_trade_enabled())
        self._voice_trade_box.setEnabled(stocks_on)
        self._voice_trade_box.blockSignals(False)
        self._auto_trade_box.blockSignals(True)
        self._auto_trade_box.setChecked(is_auto_trade_enabled())
        self._auto_trade_box.setEnabled(stocks_on)
        self._auto_trade_box.blockSignals(False)
        self._stocks_menu.setVisible(stocks_on)

    def _on_stocks_toggled(self, checked: bool) -> None:
        self._sync_stocks_menu(checked)
