# skills/crypto/settings/ui.py
# Окно параметров стола: Linear — основной контур; спот — архив/мониторинг.

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from theme_colors import current_palette, load_palette

from skills.crypto.linear import settings_store as linear_store
from skills.crypto.linear.settings_store import LinearSettings

from . import store as spot_store


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


class DeskSettingsWindow(QWidget):
    """Linear USDT Perps + спот-архив. Пишется сразу, без перезапуска."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Параметры стола")
        self.setMinimumWidth(980)
        self.setMinimumHeight(560)
        self.resize(1020, 680)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._spins: dict[str, QAbstractSpinBox] = {}
        self._hints: dict[str, str] = {}
        self._combos: dict[str, QComboBox] = {}
        self._lines: dict[str, QLineEdit] = {}
        self._desk_box: ToggleSwitch | None = None
        self._paper_box: ToggleSwitch | None = None
        self._shadow_box: ToggleSwitch | None = None
        self._pocket_box: ToggleSwitch | None = None
        self._syncing = False
        self._apply_theme()

        body = QWidget()
        body.setObjectName("root")
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 14, 10, 14)
        root.setSpacing(8)

        title = QLabel("Параметры стола")
        title.setObjectName("section")
        lead = QLabel(
            "Bybit USDT Perps (linear) — основной контур. "
            "Спот-алго в архиве; базовый слой (WS, reconcile, Earn) живой."
        )
        lead.setObjectName("lead")
        lead.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(lead)

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
        cards.addWidget(self._linear_card(), 3)
        cards.addWidget(self._spot_archive_card(), 2)
        root.addLayout(cards)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 6, 0, 0)
        foot.addStretch(1)
        reset_btn = QPushButton("Сбросить к рабочим")
        reset_btn.setObjectName("primaryBtn")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._reset)
        foot.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(foot)
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
QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{
    padding: 4px 8px;
    min-height: 20px;
    max-height: 28px;
}}
"""
        )

    def _reload(self) -> None:
        data = linear_store.current()
        spot = spot_store.current()
        self._syncing = True
        try:
            if self._desk_box is not None:
                self._desk_box.setChecked(data.desk_enabled)
            if self._paper_box is not None:
                self._paper_box.setChecked(data.paper)
            if self._shadow_box is not None:
                self._shadow_box.setChecked(data.shadow_on)
            self._set_combo("sizing_mode", data.sizing_mode)
            self._set_combo("postonly_policy", data.postonly_policy)
            self._set_line("symbols", data.symbols)
            self._set_spin("leg_usd", data.leg_usd)
            self._set_spin("target_risk_usd", data.target_risk_usd)
            self._set_spin("leg_min_usd", data.leg_min_usd)
            self._set_spin("leg_max_usd", data.leg_max_usd)
            self._set_spin("start_equity_usd", data.start_equity_usd)
            self._set_spin("uta_reserve_usd", data.uta_reserve_usd)
            self._set_spin("kill_daily_loss_usd", data.kill_daily_loss_usd)
            self._set_spin("kill_equity_floor_usd", data.kill_equity_floor_usd)
            if self._pocket_box is not None:
                self._pocket_box.setChecked(spot.pocket_on)
        finally:
            self._syncing = False
        self._refresh_sizing_enabled(data)
        self._status.setText(self._combined_status())
        self._hint.setText(
            "Paper = без боевых ордеров. Live — risk_clamp и kill-пороги."
        )

    def _combined_status(self) -> str:
        return f"{linear_store.status_line()}  ·  {spot_store.status_line()}"

    def _set_spin(self, key: str, value: float) -> None:
        spin = self._spins.get(key)
        if spin is None:
            return
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    def _set_combo(self, key: str, value: str) -> None:
        combo = self._combos.get(key)
        if combo is None:
            return
        idx = combo.findData(value)
        if idx < 0:
            idx = 0
        combo.blockSignals(True)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _set_line(self, key: str, value: str) -> None:
        line = self._lines.get(key)
        if line is None:
            return
        line.blockSignals(True)
        line.setText(value)
        line.blockSignals(False)

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

    def _heading_with_toggle(
        self, card: QFrame, box: ToggleSwitch, on_toggled
    ) -> None:
        top = card.layout()
        assert isinstance(top, QVBoxLayout)
        heading = top.itemAt(0).widget()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if heading is not None:
            top.removeWidget(heading)
            row.addWidget(heading, 1)
        box.toggled.connect(on_toggled)
        row.addWidget(box, 0)
        top.insertLayout(0, row)

    def _linear_card(self) -> QFrame:
        card, col = self._card(
            "Linear USDT Perps",
            "Core-2 ZEC+HYPE · +H1a/H1b+H2 · TP 2.5R · paper fill 0.02%",
        )
        desk = ToggleSwitch()
        desk.setChecked(True)
        self._desk_box = desk
        self._heading_with_toggle(card, desk, self._on_desk_toggled)

        paper_row = QHBoxLayout()
        paper_row.setContentsMargins(0, 4, 0, 0)
        paper_name = QLabel("Paper (без боевых ордеров)")
        paper_name.setObjectName("rowTitle")
        paper = ToggleSwitch()
        paper.setChecked(True)
        paper.toggled.connect(self._on_paper_toggled)
        self._paper_box = paper
        paper_row.addWidget(paper_name, 1)
        paper_row.addWidget(paper, 0)
        col.addLayout(paper_row)

        shadow_row = QHBoxLayout()
        shadow_row.setContentsMargins(0, 8, 0, 0)
        shadow_name = QLabel("Shadow Core-3b (+AAVE)")
        shadow_name.setObjectName("rowTitle")
        shadow = ToggleSwitch()
        shadow.setChecked(True)
        shadow.toggled.connect(self._on_shadow_toggled)
        self._shadow_box = shadow
        shadow_row.addWidget(shadow_name, 1)
        shadow_row.addWidget(shadow, 0)
        col.addLayout(shadow_row)

        self._add_line(
            col,
            "symbols",
            "Символы Core",
            "ZECUSDT,HYPEUSDT",
            "Через запятую. По умолчанию Core-2.",
        )
        self._add_combo(
            col,
            "sizing_mode",
            "Сайзинг",
            [
                ("fixed", "Фикс $"),
                ("risk_clamp", "Risk-clamp"),
            ],
            "Paper обычно фикс $40. Live — risk_clamp под target risk.",
        )
        self._add_spin(
            col, "leg_usd", "Нога", "$", 10, 100, 5, 0,
            "Фиксированный размер ноги в режиме fixed.",
        )
        self._add_spin(
            col, "target_risk_usd", "Target risk", "$", 0.2, 3.0, 0.05, 2,
            "Риск на ногу в risk_clamp (примерно SL-расстояние × размер).",
        )
        self._add_spin(
            col, "leg_min_usd", "Нога min", "$", 10, 80, 5, 0,
            "Нижняя граница размера в risk_clamp.",
        )
        self._add_spin(
            col, "leg_max_usd", "Нога max", "$", 20, 150, 5, 0,
            "Верхняя граница размера в risk_clamp.",
        )
        self._add_spin(
            col, "start_equity_usd", "Старт equity", "$", 50, 500, 10, 0,
            "Paper: виртуальный баланс (без реальных средств). Live игнор — берём с биржи.",
        )
        self._add_spin(
            col, "uta_reserve_usd", "UTA резерв", "$", 50, 500, 10, 0,
            "Не трогать под Earn/резерв; торговый буфер UTA (live).",
        )
        self._add_spin(
            col, "kill_daily_loss_usd", "Kill день", "$", 2, 40, 1, 0,
            "Дневной стоп контура. Сработал — новых входов нет.",
        )
        self._add_spin(
            col, "kill_equity_floor_usd", "Kill equity", "$", 50, 500, 5, 0,
            "Пол equity: должен быть ниже старта (иначе paper сразу kill).",
        )
        self._add_combo(
            col,
            "postonly_policy",
            "PostOnly reject",
            [
                ("reprice_1tick", "Reprice 1 tick"),
                ("skip", "Skip"),
            ],
            "Если PostOnly отклонили: сдвинуть на тик или пропустить.",
        )
        col.addStretch(1)
        return card

    def _spot_archive_card(self) -> QFrame:
        card, col = self._card(
            "Спот (архив)",
            "Алго щупа/карманов сняты. Остались мониторинг, reconcile, Earn и выходы хвостов.",
        )
        pocket = ToggleSwitch()
        pocket.setChecked(False)
        self._pocket_box = pocket
        self._heading_with_toggle(card, pocket, self._on_pocket_toggled)

        note = QLabel(
            "Код алго: `_archive/spot_desk_20261124/`.\n"
            "Новых покупок нет (`probe_places_buys=False`).\n"
            "Тумблер — только разрешение выхода/мониторинга SOL-хвоста."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setContentsMargins(0, 8, 0, 0)
        col.addWidget(note)
        col.addStretch(1)
        return card

    def _last_row_is_field(self, parent: QVBoxLayout) -> bool:
        for index in range(parent.count() - 1, -1, -1):
            widget = parent.itemAt(index).widget()
            if widget is None:
                continue
            return widget.objectName() == "cardRow"
        return False

    def _maybe_divider(self, parent: QVBoxLayout) -> None:
        if self._last_row_is_field(parent):
            line = QFrame()
            line.setObjectName("fieldLine")
            line.setFrameShape(QFrame.Shape.NoFrame)
            parent.addWidget(line)

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
        self._maybe_divider(parent)
        row = QFrame()
        row.setObjectName("cardRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("rowTitle")
        name.setWordWrap(False)
        name.setMinimumWidth(120)
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

    def _add_combo(
        self,
        parent: QVBoxLayout,
        key: str,
        label: str,
        choices: list[tuple[str, str]],
        hint: str,
    ) -> None:
        self._maybe_divider(parent)
        row = QFrame()
        row.setObjectName("cardRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("rowTitle")
        name.setMinimumWidth(120)
        layout.addWidget(name, 1)
        combo = QComboBox()
        for value, text in choices:
            combo.addItem(text, value)
        combo.setFixedWidth(140)
        combo.installEventFilter(self)
        combo.currentIndexChanged.connect(lambda _i, field=key: self._on_combo(field))
        self._combos[key] = combo
        self._hints[key] = hint
        layout.addWidget(combo, 0)
        parent.addWidget(row)

    def _add_line(
        self,
        parent: QVBoxLayout,
        key: str,
        label: str,
        placeholder: str,
        hint: str,
    ) -> None:
        self._maybe_divider(parent)
        row = QFrame()
        row.setObjectName("cardRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("rowTitle")
        name.setMinimumWidth(120)
        layout.addWidget(name, 1)
        line = QLineEdit()
        line.setPlaceholderText(placeholder)
        line.setMinimumWidth(180)
        line.installEventFilter(self)
        line.editingFinished.connect(lambda field=key: self._on_line(field))
        self._lines[key] = line
        self._hints[key] = hint
        layout.addWidget(line, 1)
        parent.addWidget(row)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            for key, spin in self._spins.items():
                if spin is watched:
                    self._show_hint(key)
                    break
            for key, combo in self._combos.items():
                if combo is watched:
                    self._show_hint(key)
                    break
            for key, line in self._lines.items():
                if line is watched:
                    self._show_hint(key)
                    break
        return super().eventFilter(watched, event)

    def _show_hint(self, key: str) -> None:
        self._hint.setText(self._hints.get(key, ""))

    def _refresh_sizing_enabled(self, data: LinearSettings) -> None:
        fixed = data.sizing_mode == "fixed"
        for key, on in (
            ("leg_usd", fixed),
            ("target_risk_usd", not fixed),
            ("leg_min_usd", not fixed),
            ("leg_max_usd", not fixed),
        ):
            spin = self._spins.get(key)
            if spin is not None:
                spin.setEnabled(on)

    def _on_spin(self, key: str) -> None:
        if self._syncing:
            return
        self._show_hint(key)
        spin = self._spins[key]
        linear_store.update(**{key: float(spin.value())})
        data = linear_store.current()
        self._syncing = True
        try:
            self._set_spin("leg_min_usd", data.leg_min_usd)
            self._set_spin("leg_max_usd", data.leg_max_usd)
        finally:
            self._syncing = False
        self._status.setText(self._combined_status())

    def _on_combo(self, key: str) -> None:
        if self._syncing:
            return
        self._show_hint(key)
        combo = self._combos[key]
        value = combo.currentData()
        linear_store.update(**{key: value})
        data = linear_store.current()
        self._refresh_sizing_enabled(data)
        self._status.setText(self._combined_status())

    def _on_line(self, key: str) -> None:
        if self._syncing:
            return
        self._show_hint(key)
        line = self._lines[key]
        linear_store.update(**{key: line.text()})
        data = linear_store.current()
        self._syncing = True
        try:
            self._set_line(key, getattr(data, key))
        finally:
            self._syncing = False
        self._status.setText(self._combined_status())

    def _on_desk_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        linear_store.update(desk_enabled=checked)
        try:
            from skills.crypto.linear.service import ensure_started, ensure_stopped

            if checked:
                ensure_started()
            else:
                ensure_stopped()
        except Exception:
            pass
        self._status.setText(self._combined_status())
        self._hint.setText(
            "Контур linear включён — фон ассистента (без Cursor)."
            if checked
            else "Контур linear выключен — новых сигналов нет."
        )

    def _on_paper_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        changes: dict[str, object] = {"paper": checked}
        if not checked:
            changes["sizing_mode"] = "risk_clamp"
        else:
            changes["sizing_mode"] = "fixed"
        data = linear_store.update(**changes)
        self._syncing = True
        try:
            self._set_combo("sizing_mode", data.sizing_mode)
        finally:
            self._syncing = False
        self._refresh_sizing_enabled(data)
        self._status.setText(self._combined_status())
        self._hint.setText(
            "Paper: журнал и fills без боевых ордеров."
            if checked
            else "LIVE: боевые ордера. Проверь kill и UTA резерв."
        )

    def _on_shadow_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        linear_store.update(shadow_on=checked)
        self._status.setText(self._combined_status())
        self._hint.setText(
            "Shadow Core-3b (+AAVE) в отдельном paper-портфеле."
            if checked
            else "Shadow выключен — только Core."
        )

    def _on_pocket_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        spot_store.update(pocket_on=checked)
        self._status.setText(self._combined_status())
        self._hint.setText(
            "Выход/мониторинг SOL-хвоста разрешён (новых входов всё равно нет)."
            if checked
            else "SOL-хвост: только наблюдение."
        )

    def _reset(self) -> None:
        linear_store.reset()
        spot_store.reset()
        self._reload()
        self._hint.setText("Вернули рабочие числа linear и спот-архива.")
