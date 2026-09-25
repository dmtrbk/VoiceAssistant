# settings_ui.py
# Окно Qt: вкладки «Основные» (речь, модель, характер) и «Навыки». Каркас скрыт.

from PySide6.QtCore import QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from skill_settings import (
    get_flags,
    get_openai_model,
    get_persona_hint,
    get_persona_preset,
    grouped_skills,
    is_auto_trade_enabled,
    is_crypto_auto_trade_enabled,
    is_crypto_voice_trade_enabled,
    is_cursor_running,
    is_voice_trade_enabled,
    ordered_skill_ids,
    persona_preset_choices,
    set_auto_trade,
    set_crypto_auto_trade,
    set_crypto_voice_trade,
    set_flag,
    set_openai_model,
    set_persona_preset,
    set_voice_trade,
)
from skills.openai_client import FAST_MODEL, openai_model_choices
from theme_colors import current_palette, load_palette


class _ModelCatalogWorker(QThread):
    """Фон: обновить каталог и опционально пропинговать free-модели."""

    finished_ok = Signal(object)  # list[tuple[str, str]]
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, *, probe: bool = False, parent=None):
        super().__init__(parent)
        self._probe = probe

    def run(self) -> None:
        try:
            from skills.model_catalog import fetch_free_models, probe_models, settings_model_choices

            self.progress.emit("Обновляю каталог OpenRouter…")
            catalog = fetch_free_models(force=True)
            if self._probe and catalog:
                ids = [mid for mid, _title in catalog]

                def on_progress(done: int, total: int, mid: str, status: str) -> None:
                    mark = "ok" if status == "ok" else "fail"
                    self.progress.emit(f"Проверка {done}/{total}: {mid} [{mark}]")

                self.progress.emit(f"Пинг {len(ids)} бесплатных моделей…")
                probe_models(ids, progress=on_progress)
            self.finished_ok.emit(settings_model_choices(get_openai_model()))
        except Exception as exc:
            self.failed.emit(str(exc))


class ToggleSwitch(QCheckBox):
    """Короткий тумблер без стандартного квадрата Qt."""

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


class SettingsWindow(QWidget):
    """Список тумблеров. Изменение сразу пишется на диск, служба не перезапускается."""

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Настройки Джарвиса")
        self.setMinimumWidth(440)
        self.setMinimumHeight(480)
        self.resize(480, 620)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._apply_theme()

        self._model_combo: QComboBox | None = None
        self._model_hint: QLabel | None = None
        self._model_refresh_btn: QPushButton | None = None
        self._model_probe_btn: QPushButton | None = None
        self._model_worker: _ModelCatalogWorker | None = None
        self._persona_combo: QComboBox | None = None
        self._persona_hint: QLabel | None = None
        self._boxes: dict[str, QCheckBox] = {}
        self._voice_trade_box: QCheckBox | None = None
        self._auto_trade_box: QCheckBox | None = None
        self._stocks_menu: QFrame | None = None
        self._crypto_voice_box: QCheckBox | None = None
        self._crypto_auto_box: QCheckBox | None = None
        self._crypto_menu: QFrame | None = None
        self._list: QVBoxLayout | None = None

        self._stack = QStackedWidget()
        self._stack.setObjectName("root")
        self._stack.addWidget(self._wrap_scroll(self._build_main_page()))
        self._stack.addWidget(self._build_skills_page())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        root.addWidget(self._build_nav())
        root.addWidget(self._stack, 1)
        self._rebuild()

    def _build_nav(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(nav)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)

        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, title in enumerate(("Основные", "Навыки")):
            button = QPushButton(title)
            button.setObjectName("navBtn")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            group.addButton(button, index)
            row.addWidget(button, 1)
        group.button(0).setChecked(True)
        group.idClicked.connect(self._stack.setCurrentIndex)
        self._nav_group = group
        return nav

    def _wrap_scroll(self, body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        return scroll

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("root")
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(10)
        col.addWidget(self._build_model_card())
        col.addWidget(self._build_persona_card())
        col.addStretch(1)
        return page

    def _build_skills_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("root")
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(8)

        subtitle = QLabel("Выключенный навык молчит и в голосе, и в Telegram.")
        subtitle.setObjectName("lead")
        subtitle.setWordWrap(True)
        col.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body = QWidget()
        body.setObjectName("root")
        self._list = QVBoxLayout(body)
        self._list.setContentsMargins(0, 0, 6, 8)
        self._list.setSpacing(16)
        scroll.setWidget(body)
        col.addWidget(scroll, 1)
        return page

    def _section_label(self, text: str) -> QLabel:
        heading = QLabel(text)
        heading.setObjectName("section")
        heading.setContentsMargins(4, 8, 0, 0)
        return heading

    def _card_title(self, text: str) -> QLabel:
        heading = QLabel(text)
        heading.setObjectName("section")
        return heading

    def _build_model_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 12, 16, 12)
        col.setSpacing(8)
        col.addWidget(self._card_title("Модель диалога"))

        combo = QComboBox()
        combo.setMaxVisibleItems(16)
        for model_id, title in openai_model_choices(get_openai_model()):
            combo.addItem(title, model_id)
        combo.currentIndexChanged.connect(self._on_model_changed)
        self._model_combo = combo
        col.addWidget(combo)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("navBtn")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh.clicked.connect(lambda: self._start_model_worker(probe=False))
        self._model_refresh_btn = refresh
        actions.addWidget(refresh, 1)
        probe = QPushButton("Проверить")
        probe.setObjectName("navBtn")
        probe.setCursor(Qt.CursorShape.PointingHandCursor)
        probe.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        probe.setToolTip(
            "Короткий ping каждой бесплатной модели (медленно, тратит лимиты free)."
        )
        probe.clicked.connect(lambda: self._start_model_worker(probe=True))
        self._model_probe_btn = probe
        actions.addWidget(probe, 1)
        col.addLayout(actions)

        hint = QLabel()
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self._model_hint = hint
        col.addWidget(hint)
        self._sync_model_row()
        self._kick_catalog_refresh()
        return card

    def _set_model_buttons_enabled(self, enabled: bool) -> None:
        for button in (self._model_refresh_btn, self._model_probe_btn):
            if button is not None:
                button.setEnabled(enabled)

    def _kick_catalog_refresh(self) -> None:
        """При открытии: тихо подтянуть каталог, если кэш пуст/протух."""
        try:
            from skills.model_catalog import cached_free_models

            if cached_free_models(allow_stale=False):
                return
        except Exception:
            return
        self._start_model_worker(probe=False)

    def _start_model_worker(self, *, probe: bool) -> None:
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        self._set_model_buttons_enabled(False)
        if self._model_hint is not None:
            self._model_hint.setText(
                "Пинг бесплатных моделей…" if probe else "Обновляю каталог OpenRouter…"
            )
        worker = _ModelCatalogWorker(probe=probe, parent=self)
        worker.progress.connect(self._on_model_worker_progress)
        worker.finished_ok.connect(self._on_model_worker_ok)
        worker.failed.connect(self._on_model_worker_fail)
        worker.finished.connect(lambda: self._set_model_buttons_enabled(True))
        self._model_worker = worker
        worker.start()

    def _on_model_worker_progress(self, text: str) -> None:
        if self._model_hint is not None and text:
            self._model_hint.setText(text)

    def _on_model_worker_ok(self, _models: object) -> None:
        self._reload_model_combo()
        self._sync_model_row()

    def _on_model_worker_fail(self, message: str) -> None:
        if self._model_hint is not None:
            self._model_hint.setText(f"Не удалось обновить модели: {message}")

    def _reload_model_combo(self) -> None:
        if self._model_combo is None:
            return
        current = get_openai_model()
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for model_id, title in openai_model_choices(current):
            self._model_combo.addItem(title, model_id)
        index = self._model_combo.findData(current)
        if index < 0 and current:
            self._model_combo.addItem(current, current)
            index = self._model_combo.findData(current)
        if index >= 0:
            self._model_combo.setCurrentIndex(index)
        self._model_combo.blockSignals(False)

    def _build_persona_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 12, 16, 12)
        col.setSpacing(8)
        col.addWidget(self._card_title("Характер ассистента"))

        combo = QComboBox()
        for preset_id, title in persona_preset_choices():
            combo.addItem(title, preset_id)
        combo.currentIndexChanged.connect(self._on_persona_changed)
        self._persona_combo = combo
        col.addWidget(combo)

        hint = QLabel()
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self._persona_hint = hint
        col.addWidget(hint)
        self._sync_persona_row()
        return card

    def _apply_theme(self) -> None:
        self.setStyleSheet(load_palette().stylesheet())

    def showEvent(self, event):
        self._apply_theme()
        self._rebuild()
        self._sync_model_row()
        self._sync_persona_row()
        super().showEvent(event)

    def _sync_persona_row(self) -> None:
        if self._persona_combo is None or self._persona_hint is None:
            return
        current = get_persona_preset()
        index = self._persona_combo.findData(current)
        self._persona_combo.blockSignals(True)
        if index >= 0:
            self._persona_combo.setCurrentIndex(index)
        self._persona_combo.blockSignals(False)
        self._persona_hint.setText(get_persona_hint(current))

    def _on_persona_changed(self, index: int) -> None:
        if self._persona_combo is None or index < 0:
            return
        preset_id = self._persona_combo.itemData(index)
        if not preset_id:
            return
        set_persona_preset(str(preset_id))
        self._sync_persona_row()

    def _sync_model_row(self) -> None:
        if self._model_combo is None or self._model_hint is None:
            return
        current = get_openai_model()
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
        try:
            from skills.model_catalog import cached_free_models

            free_n = len(cached_free_models(allow_stale=True))
        except Exception:
            free_n = 0
        catalog_note = (
            f"В списке {free_n} бесплатных с OpenRouter. "
            "«Обновить список» — каталог, «Проверить» — ping (медленно)."
            if free_n
            else "Нажми «Обновить список», чтобы подтянуть бесплатные модели OpenRouter."
        )
        if cursor_on and strong:
            self._model_hint.setText(
                "Сильная выбрана. Пока открыт Cursor, отвечает быстрая. "
                "Закрой редактор — Джарвис переключится. " + catalog_note
            )
        elif cursor_on:
            self._model_hint.setText(
                "Сейчас быстрая. Сильную можно выбрать заранее: заработает, когда закроешь Cursor. "
                + catalog_note
            )
        elif strong:
            self._model_hint.setText(
                "Сильная модель. Если откроешь Cursor, временно вернётся быстрая. " + catalog_note
            )
        else:
            self._model_hint.setText(
                "Быстрая модель. Сильную удобнее включать, когда Cursor закрыт. " + catalog_note
            )

    def _on_model_changed(self, index: int) -> None:
        if self._model_combo is None or index < 0:
            return
        model_id = self._model_combo.itemData(index)
        if not model_id:
            return
        set_openai_model(str(model_id))
        self._sync_model_row()

    def _make_toggle(self, checked: bool) -> QCheckBox:
        box = ToggleSwitch()
        box.setChecked(checked)
        return box

    def _add_text_toggle(
        self,
        parent: QVBoxLayout,
        title: str,
        hint: str,
        box: QCheckBox,
    ) -> None:
        row = QFrame()
        row.setObjectName("cardRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(12)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("rowTitle")
        name.setWordWrap(True)
        caption = QLabel(hint)
        caption.setObjectName("hint")
        caption.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(caption)
        layout.addLayout(text, 1)
        layout.addWidget(box, 0, Qt.AlignmentFlag.AlignVCenter)
        parent.addWidget(row)

    def _add_divider(self, parent: QVBoxLayout) -> None:
        line = QFrame()
        line.setObjectName("divider")
        line.setFrameShape(QFrame.Shape.NoFrame)
        parent.addWidget(line)

    def _rebuild(self) -> None:
        if self._list is None:
            return
        flags = get_flags()
        expected = ordered_skill_ids()
        if (
            self._boxes
            and list(self._boxes.keys()) == expected
            and self._voice_trade_box is not None
            and self._auto_trade_box is not None
            and self._crypto_voice_box is not None
            and self._crypto_auto_box is not None
        ):
            for skill_id, box in self._boxes.items():
                box.blockSignals(True)
                box.setChecked(flags.get(skill_id, True))
                box.blockSignals(False)
            self._sync_stocks_menu(flags.get("stocks", True))
            self._sync_crypto_menu(flags.get("crypto", True))
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
        self._crypto_voice_box = None
        self._crypto_auto_box = None
        self._crypto_menu = None

        for group_name, rows in grouped_skills():
            self._list.addWidget(self._section_label(group_name))
            card = QFrame()
            card.setObjectName("card")
            card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            col = QVBoxLayout(card)
            col.setContentsMargins(16, 8, 16, 8)
            col.setSpacing(0)

            for index, (skill_id, title, hint) in enumerate(rows):
                if index:
                    self._add_divider(col)
                box = self._make_toggle(flags.get(skill_id, True))
                box.toggled.connect(lambda checked, sid=skill_id: set_flag(sid, checked))
                if skill_id == "stocks":
                    box.toggled.connect(self._on_stocks_toggled)
                if skill_id == "crypto":
                    box.toggled.connect(self._on_crypto_toggled)
                self._boxes[skill_id] = box
                self._add_text_toggle(col, title, hint, box)
                if skill_id == "stocks":
                    self._add_stocks_menu(col, flags.get("stocks", True))
                if skill_id == "crypto":
                    self._add_crypto_menu(col, flags.get("crypto", True))

            self._list.addWidget(card)

        self._list.addStretch(1)

    def _add_stocks_menu(self, parent: QVBoxLayout, stocks_on: bool) -> None:
        menu = QFrame()
        menu.setObjectName("submenu")
        menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(menu)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(8)

        voice_box = self._make_toggle(is_voice_trade_enabled())
        voice_box.toggled.connect(set_voice_trade)
        self._add_text_toggle(
            col,
            "Сделки голосом",
            "«Купи», «продай», «поторгуй» выставляют заявки. Выключено — только сводка.",
            voice_box,
        )

        self._add_divider(col)

        auto_box = self._make_toggle(is_auto_trade_enabled())
        auto_box.toggled.connect(set_auto_trade)
        self._add_text_toggle(
            col,
            "Автоторговля",
            "Фон: стол ~3 ч (сигналы), дозор ~15 мин к сохранённой цели. На живом счёте — реальные сделки.",
            auto_box,
        )

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

    def _add_crypto_menu(self, parent: QVBoxLayout, crypto_on: bool) -> None:
        menu = QFrame()
        menu.setObjectName("submenu")
        menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(menu)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(8)

        voice_box = self._make_toggle(is_crypto_voice_trade_enabled())
        voice_box.toggled.connect(set_crypto_voice_trade)
        self._add_text_toggle(
            col,
            "Сделки голосом",
            "«Купи биткоин» ставит спот на Bybit. Выключено — только курс.",
            voice_box,
        )

        self._add_divider(col)

        auto_box = self._make_toggle(is_crypto_auto_trade_enabled())
        auto_box.toggled.connect(set_crypto_auto_trade)
        self._add_text_toggle(
            col,
            "Автоторговля",
            "Фон: стол ~6 ч, дозор ~12 мин (трейл / TP / риск-офф / добор ядра). «Посоветуй» — отдельно через ИИ.",
            auto_box,
        )

        self._crypto_voice_box = voice_box
        self._crypto_auto_box = auto_box
        self._crypto_menu = menu
        parent.addWidget(menu)
        self._sync_crypto_menu(crypto_on)

    def _sync_crypto_menu(self, crypto_on: bool) -> None:
        if (
            self._crypto_voice_box is None
            or self._crypto_auto_box is None
            or self._crypto_menu is None
        ):
            return
        self._crypto_voice_box.blockSignals(True)
        self._crypto_voice_box.setChecked(is_crypto_voice_trade_enabled())
        self._crypto_voice_box.setEnabled(crypto_on)
        self._crypto_voice_box.blockSignals(False)
        self._crypto_auto_box.blockSignals(True)
        self._crypto_auto_box.setChecked(is_crypto_auto_trade_enabled())
        self._crypto_auto_box.setEnabled(crypto_on)
        self._crypto_auto_box.blockSignals(False)
        self._crypto_menu.setVisible(crypto_on)

    def _on_crypto_toggled(self, checked: bool) -> None:
        self._sync_crypto_menu(checked)
