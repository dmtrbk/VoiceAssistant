# skills/crypto/settings/ui.py
# Отдельное окно порогов стола. Тема как у настроек Джарвиса.

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from theme_colors import current_palette, load_palette

from .schema import DeskSettings
from .store import current, reset, status_line, update


class ToggleSwitch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 26)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def hitButton(self, pos):
        return self.contentsRect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(1, 2, -1, -2)
        palette = current_palette()
        if not self.isEnabled():
            track_color = QColor(palette.divider)
            knob_color = QColor(palette.muted)
        elif self.isChecked():
            track_color = QColor(palette.accent)
            knob_color = QColor(palette.on_knob)
        else:
            track_color = QColor(palette.off_track)
            knob_color = QColor(palette.off_knob)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = 18
        y = (self.height() - knob) / 2
        x = self.width() - knob - 4 if self.isChecked() else 4
        painter.setBrush(knob_color)
        painter.drawEllipse(QRectF(x, y, knob, knob))
        painter.end()


class AllocBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self._pocket = 25.0
        self._probe_on = True

    def set_shares(self, pocket: float, probe_on: bool) -> None:
        self._pocket = max(0.0, float(pocket))
        self._probe_on = bool(probe_on)
        self.setToolTip(self._label())
        self.update()

    def _label(self) -> str:
        cash = max(0.0, 100.0 - self._pocket)
        text = f"Карман SOL до {self._pocket:.0f}% счёта, остальное кэш и щупы"
        if self._probe_on:
            return text
        return text + " (щуп выкл)"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = current_palette()
        gap = 3
        height = self.height()
        width = self.width()
        cash = max(0.0, 100.0 - self._pocket)
        parts = [("pocket", self._pocket, QColor(palette.accent))]
        if cash > 0.5:
            parts.append(("cash", cash, QColor(palette.divider)))
        if self._probe_on:
            parts.append(("probe", 6.0, QColor(palette.off_track)))
        total = sum(share for _name, share, _color in parts) or 1.0
        gaps = gap * (len(parts) - 1)
        usable = max(0, width - gaps)
        x = 0.0
        painter.setPen(Qt.PenStyle.NoPen)
        for index, (_name, share, color) in enumerate(parts):
            seg = usable * (share / total)
            if index == len(parts) - 1:
                seg = max(0.0, width - x)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, 0, max(6.0, seg), height), height / 2, height / 2)
            x += seg + gap
        painter.end()


class DeskSettingsWindow(QWidget):
    """Пороги кармана и щупа. Пишется сразу, стол не перезапускается."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Параметры стола")
        self.setMinimumWidth(1040)
        self.setMinimumHeight(560)
        self.resize(1080, 640)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._spins: dict[str, QAbstractSpinBox] = {}
        self._hints: dict[str, str] = {}
        self._probe_box: ToggleSwitch | None = None
        self._pocket_box: ToggleSwitch | None = None
        self._second_box: ToggleSwitch | None = None
        self._second_combo = None
        self._syncing = False
        self._apply_theme()

        body = QWidget()
        body.setObjectName("root")
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 14, 10, 14)
        root.setSpacing(8)

        title = QLabel("Параметры стола")
        title.setObjectName("section")
        lead = QLabel("Пишется сразу. Стол подхватывает без перезапуска.")
        lead.setObjectName("lead")
        lead.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(lead)

        alloc = QVBoxLayout()
        alloc.setContentsMargins(0, 8, 0, 0)
        alloc.setSpacing(10)
        self._bar = AllocBar()
        alloc.addWidget(self._bar)
        legend = QHBoxLayout()
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(16)
        self._pocket_legend = QLabel()
        self._probe_legend = QLabel()
        self._pocket_legend.setTextFormat(Qt.TextFormat.RichText)
        self._probe_legend.setTextFormat(Qt.TextFormat.RichText)
        self._probe_legend.setObjectName("chip")
        legend.addWidget(self._pocket_legend)
        legend.addStretch(1)
        legend.addWidget(self._probe_legend)
        alloc.addLayout(legend)
        root.addLayout(alloc)

        self._status = QLabel()
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._hint = QLabel()
        self._hint.setObjectName("hint")
        self._hint.setWordWrap(True)
        self._hint.setMinimumHeight(34)
        root.addWidget(self._hint)

        cards = QHBoxLayout()
        cards.setContentsMargins(0, 6, 0, 0)
        cards.setSpacing(10)
        cards.addWidget(self._pocket_card(), 1)
        cards.addWidget(self._probe_card(), 1)
        root.addLayout(cards)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 6, 0, 0)
        extra_btn = QPushButton("Ещё")
        extra_btn.setObjectName("linkBtn")
        extra_btn.setCheckable(True)
        extra_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        extra_btn.toggled.connect(self._toggle_extra)
        self._extra_btn = extra_btn
        foot.addWidget(extra_btn, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        foot.addStretch(1)
        reset_btn = QPushButton("Сбросить к рабочим")
        reset_btn.setObjectName("primaryBtn")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._reset)
        foot.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(foot)

        extra_row = QHBoxLayout()
        extra_row.setContentsMargins(0, 0, 0, 0)
        self._extra = self._extra_box()
        self._extra.setFixedWidth(460)
        self._extra.setVisible(False)
        extra_row.addWidget(self._extra, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        extra_row.addStretch(1)
        root.addLayout(extra_row)
        root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setWidget(body)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(0)
        wrap.addWidget(scroll)
        self._reload()

    def showEvent(self, event):
        self._apply_theme()
        self._reload()
        super().showEvent(event)

    def _apply_theme(self) -> None:
        palette = load_palette()
        self.setStyleSheet(
            palette.stylesheet()
            + f"""
QFrame#fieldLine {{
    background: {palette.divider};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QSpinBox, QDoubleSpinBox {{
    padding: 4px 8px;
    min-height: 20px;
    max-height: 28px;
}}
"""
        )

    def _reload(self) -> None:
        data = current()
        self._syncing = True
        try:
            if self._pocket_box is not None:
                self._pocket_box.setChecked(data.pocket_on)
            self._set_spin("pocket_pct", data.pocket_pct)
            self._set_spin("pocket_day_pct", data.pocket_day_pct)
            self._set_spin("pocket_bounce_pct", data.pocket_bounce_pct)
            self._set_spin("pocket_stop_pct", data.pocket_stop_pct)
            self._set_spin("pocket_from_probe_max", data.pocket_from_probe_max)
            if self._second_box is not None:
                self._second_box.setChecked(data.second_on)
            self._set_spin("probe_usd", data.probe_usd)
            self._set_spin("probe_max_loss_pct", data.probe_max_loss_pct)
            self._set_spin("probe_shelf_range_pct", data.probe_shelf_range_pct)
            self._set_spin("probe_break_min_pct", data.probe_break_min_pct)
            self._set_spin("probe_break_max_pct", data.probe_break_max_pct)
            self._set_spin("pocket_hold_min", data.pocket_hold_sec / 60.0)
            self._set_spin("pocket_exit_pullback_pct", data.pocket_exit_pullback_pct)
            self._set_spin("cooldown_hours", data.cooldown_hours)
            if self._probe_box is not None:
                self._probe_box.setChecked(data.probe_on)
        finally:
            self._syncing = False
        self._refresh_alloc(data)
        self._set_probe_enabled(data.probe_on)
        self._set_pocket_enabled(data.pocket_on)
        self._status.setText(status_line())
        self._hint.setText("Карман SOL — доля счёта. Щуп — фикс. $ на свечу.")

    def _refresh_alloc(self, data: DeskSettings) -> None:
        self._bar.set_shares(data.pocket_pct, data.probe_on)
        self._pocket_legend.setText(f"Карман <b>{data.pocket_pct:.0f}%</b>")
        self._probe_legend.setText(f"Щуп <b>${data.probe_usd:.0f}</b>")
        self._probe_legend.setVisible(data.probe_on)

    def _set_spin(self, key: str, value: float) -> None:
        spin = self._spins.get(key)
        if spin is None:
            return
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    def _card(self, title: str, why: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 14, 16, 12)
        col.setSpacing(0)
        heading = QLabel(title)
        heading.setObjectName("section")
        caption = QLabel(why)
        caption.setObjectName("hint")
        caption.setWordWrap(True)
        caption.setContentsMargins(0, 2, 0, 8)
        col.addWidget(heading)
        col.addWidget(caption)
        return card, col

    def _pocket_card(self) -> QFrame:
        card, col = self._card("Карман SOL", "Откат и доля счёта; карман 2 — ноги с щупа")
        top = card.layout()
        assert isinstance(top, QVBoxLayout)
        heading = top.itemAt(0).widget()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if heading is not None:
            top.removeWidget(heading)
            row.addWidget(heading, 1)
        box = ToggleSwitch()
        box.setChecked(False)
        box.toggled.connect(self._on_pocket_toggled)
        self._pocket_box = box
        row.addWidget(box, 0)
        top.insertLayout(0, row)
        self._add_spin(col, "pocket_pct", "Доля", "%", 8, 50, 1, 0, "Максимум счёта на карман SOL (пока позиция открыта).")
        self._add_spin(col, "pocket_day_pct", "Просадка дня", "%", -15, -1, 0.5, 1, "Старый порог суток. Вход кармана сейчас — откат 3% от верха 4 часов.")
        self._add_spin(col, "pocket_bounce_pct", "Отскок входа", "%", 0.3, 5, 0.1, 1, "+1% от локального дна. Покупка после закрытия следующей 15м выше этого дна.")
        self._add_spin(col, "pocket_stop_pct", "Стоп", "%", 5, 30, 1, 0, "Запасной стоп на дыре. Обычный срыв кармана — возврат под локальное дно.")
        switch = QHBoxLayout()
        switch.setContentsMargins(0, 8, 0, 0)
        name = QLabel("Карман 2 с щупа")
        name.setObjectName("rowTitle")
        box = ToggleSwitch()
        box.setChecked(False)
        box.toggled.connect(self._on_second_toggled)
        self._second_box = box
        switch.addWidget(name, 1)
        switch.addWidget(box, 0)
        col.addLayout(switch)
        self._second_combo = None
        self._add_spin(
            col,
            "pocket_from_probe_max",
            "Ног с щупа",
            "",
            1,
            6,
            1,
            0,
            "Сколько переданных с щупа ног держим. Тикер любой — без AVA/SUI. Полон — щуп продаёт.",
        )
        col.addStretch(1)
        return card

    def _probe_card(self) -> QFrame:
        card, col = self._card("Щуп", "5м зелёная свеча в начале хода — основной инструмент стола")
        top = card.layout()
        assert isinstance(top, QVBoxLayout)
        heading = top.itemAt(0).widget()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if heading is not None:
            top.removeWidget(heading)
            row.addWidget(heading, 1)
        box = ToggleSwitch()
        box.setChecked(True)
        box.toggled.connect(self._on_probe_toggled)
        self._probe_box = box
        row.addWidget(box, 0)
        top.insertLayout(0, row)
        self._probe_fields = QFrame()
        self._probe_fields.setObjectName("cardRow")
        fields = QVBoxLayout(self._probe_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(0)
        self._add_spin(
            fields,
            "probe_usd",
            "Размер свечи",
            "$",
            10,
            100,
            5,
            0,
            "Одна покупка щупа. Неудачная свеча может уйти во второй карман.",
        )
        self._add_spin(fields, "probe_max_loss_pct", "Макс. убыток", "%", 3, 20, 0.5, 1, "Запасной стоп на дыре. До хода +6% щуп также выходит под минимум свечи входа.")
        self._add_spin(fields, "probe_shelf_range_pct", "Полка, ширина", "%", 4, 25, 1, 0, "Диапазон полки не шире этого, иначе сетап не считается.")
        self._add_spin(fields, "probe_break_min_pct", "Пробой от", "%", 1, 10, 0.5, 1, "Цена должна выйти из полки хотя бы на этот процент.")
        self._add_spin(fields, "probe_break_max_pct", "Пробой до", "%", 6, 25, 0.5, 1, "Выше этого — уже не полка, а разогнанный памп.")
        col.addWidget(self._probe_fields)
        return card

    def _toggle_extra(self, opened: bool) -> None:
        self._extra.setVisible(opened)

    def _extra_box(self) -> QFrame:
        box = QFrame()
        box.setObjectName("card")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(box)
        col.setContentsMargins(16, 8, 16, 8)
        col.setSpacing(0)
        self._add_spin(col, "pocket_hold_min", "Карман, пауза входа", "мин", 2, 40, 1, 0, "Сколько минут дно кармана должно держаться перед покупкой.")
        self._add_spin(col, "pocket_exit_pullback_pct", "Карман, откат выхода", "%", 0.2, 3, 0.1, 1, "Продажа кармана: откат от вершины после короткой паузы.")
        self._add_spin(col, "cooldown_hours", "Кулдаун щупа", "ч", 1, 48, 1, 0, "После убыточной продажи щуп не возвращается в ту же монету сразу.")
        return box

    def _last_row_is_field(self, parent: QVBoxLayout) -> bool:
        for index in range(parent.count() - 1, -1, -1):
            widget = parent.itemAt(index).widget()
            if widget is None:
                continue
            return widget.objectName() == "cardRow"
        return False

    def _add_spin(
        self,
        parent: QVBoxLayout,
        key: str,
        label: str,
        unit: str,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        hint: str,
    ) -> None:
        if self._last_row_is_field(parent):
            line = QFrame()
            line.setObjectName("fieldLine")
            line.setFrameShape(QFrame.Shape.NoFrame)
            parent.addWidget(line)
        row = QFrame()
        row.setObjectName("cardRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("rowTitle")
        name.setWordWrap(False)
        name.setMinimumWidth(140)
        layout.addWidget(name, 1)
        if decimals <= 0 and abs(step - int(step)) < 1e-9:
            spin: QAbstractSpinBox = QSpinBox()
            spin.setRange(int(minimum), int(maximum))
            spin.setSingleStep(int(step))
        else:
            spin = QDoubleSpinBox()
            spin.setRange(float(minimum), float(maximum))
            spin.setSingleStep(float(step))
            spin.setDecimals(decimals)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setFixedWidth(78)
        spin.installEventFilter(self)
        spin.valueChanged.connect(lambda _value, field=key: self._on_spin(field))
        spin.editingFinished.connect(lambda field=key: self._show_hint(field))
        self._spins[key] = spin
        self._hints[key] = hint
        layout.addWidget(spin, 0)
        if unit:
            suffix = QLabel(unit)
            suffix.setObjectName("hint")
            suffix.setFixedWidth(28)
            layout.addWidget(suffix, 0)
        parent.addWidget(row)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            for key, spin in self._spins.items():
                if spin is watched:
                    self._show_hint(key)
                    break
        return super().eventFilter(watched, event)

    def _show_hint(self, key: str) -> None:
        self._hint.setText(self._hints.get(key, ""))

    def _on_spin(self, key: str) -> None:
        if self._syncing:
            return
        self._show_hint(key)
        spin = self._spins[key]
        value = float(spin.value())
        if key == "pocket_hold_min":
            update(pocket_hold_sec=value * 60.0)
        else:
            update(**{key: value})
        data = current()
        self._syncing = True
        try:
            self._set_spin("pocket_pct", data.pocket_pct)
            self._set_spin("probe_break_min_pct", data.probe_break_min_pct)
            self._set_spin("probe_break_max_pct", data.probe_break_max_pct)
        finally:
            self._syncing = False
        self._refresh_alloc(data)

    def _on_pocket_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        update(pocket_on=checked)
        data = current()
        self._set_pocket_enabled(data.pocket_on)
        self._refresh_alloc(data)
        self._status.setText(status_line())
        self._hint.setText(
            "Карман SOL ищет откат."
            if checked
            else "SOL выключен — только выход по открытой ноге, новых входов нет."
        )

    def _set_pocket_enabled(self, on: bool) -> None:
        for key in (
            "pocket_pct",
            "pocket_day_pct",
            "pocket_bounce_pct",
            "pocket_stop_pct",
            "pocket_hold_min",
            "pocket_exit_pullback_pct",
        ):
            spin = self._spins.get(key)
            if spin is not None:
                spin.setEnabled(on)

    def _on_second_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        update(second_on=checked)
        if not checked:
            self._clear_second_arm_if_flat()
        self._status.setText(status_line())
        self._hint.setText(
            "Второй карман принимает ноги щупа, если свеча не задалась."
            if checked
            else "Второй карман выключен — щуп продаёт сам."
        )

    def _clear_second_arm_if_flat(self) -> None:
        from skills.crypto.common import read_entry_arms, read_second_pocket_legs, write_entry_arms

        if read_second_pocket_legs():
            return
        arms = read_entry_arms()
        write_entry_arms({}, arms.get("pocket"), sui=False)

    def _on_probe_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        update(probe_on=checked)
        data = current()
        self._set_probe_enabled(data.probe_on)
        self._refresh_alloc(data)
        self._status.setText(status_line())
        self._hint.setText("Щуп сканирует ленту." if data.probe_on else "Щуп выключен.")

    def _set_probe_enabled(self, on: bool) -> None:
        for key in (
            "probe_usd",
            "probe_max_loss_pct",
            "probe_shelf_range_pct",
            "probe_break_min_pct",
            "probe_break_max_pct",
        ):
            spin = self._spins.get(key)
            if spin is not None:
                spin.setEnabled(on)

    def _reset(self) -> None:
        reset()
        self._reload()
        self._hint.setText("Вернули рабочие числа стола.")
