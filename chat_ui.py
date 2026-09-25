# chat_ui.py
# Компактное окно чата: ~15 строк истории, Esc/крестик закрывают чат (не приложение).

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from theme_colors import current_palette, load_palette


class ChatWindow(QWidget):
    """Текстовый чат поверх маршрутизатора commands.execute (без TTS)."""

    closed = Signal()
    _finished = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Чат Джарвиса")
        self.setMinimumWidth(340)
        self.resize(400, 400)
        self._busy = False
        self._closing = False
        self._finished.connect(self._on_finished)

        self._apply_theme()

        self._log = QTextBrowser()
        self._log.setObjectName("chatLog")
        self._log.setOpenExternalLinks(True)
        self._log.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        line = max(18, self.fontMetrics().lineSpacing())
        self._log.setMinimumHeight(line * 15)

        self._input = QLineEdit()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText("Напишите сообщение…")
        self._input.returnPressed.connect(self._send)

        self._send_btn = QPushButton("Отправить")
        self._send_btn.setObjectName("chatSend")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.clicked.connect(self._send)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._input, 1)
        row.addWidget(self._send_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        root.addWidget(self._log, 1)
        root.addLayout(row)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

    def _apply_theme(self) -> None:
        palette = load_palette()
        extra = f"""
QTextBrowser#chatLog {{
    background: {palette.card};
    color: {palette.fg};
    border: 1px solid {palette.divider};
    border-radius: 10px;
    padding: 10px;
    font-size: 13px;
}}
QLineEdit#chatInput {{
    background: {palette.card};
    color: {palette.fg};
    border: 1px solid {palette.divider};
    border-radius: 8px;
    padding: 8px 10px;
    min-height: 28px;
}}
QLineEdit#chatInput:focus {{ border-color: {palette.accent}; }}
QPushButton#chatSend {{
    background: {palette.accent};
    color: {palette.accent_fg};
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 500;
}}
QPushButton#chatSend:disabled {{
    background: {palette.off_track};
    color: {palette.muted};
}}
"""
        self.setStyleSheet(palette.stylesheet() + extra)

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_theme()
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def closeEvent(self, event):
        if not self._closing:
            self._closing = True
            self.closed.emit()
        event.accept()

    def focus_input(self) -> None:
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def prepare_reopen(self) -> None:
        """Сброс флага перед повторным show() после close()."""
        self._closing = False

    def _append(self, who: str, text: str, *, muted: bool = False) -> None:
        palette = current_palette()
        color = palette.muted if muted else (palette.accent if who == "Вы" else palette.fg)
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        self._log.append(
            f'<p style="margin:0 0 8px 0;">'
            f'<span style="color:{color}; font-weight:600;">{who}:</span> {safe}</p>'
        )
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._input.setEnabled(not busy)
        self._send_btn.setEnabled(not busy)
        if busy:
            self._input.setPlaceholderText("Думаю…")
        else:
            self._input.setPlaceholderText("Напишите сообщение…")
            self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _send(self) -> None:
        if self._busy:
            return
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._append("Вы", text)
        self._set_busy(True)

        thread = threading.Thread(
            target=self._run_command,
            args=(text,),
            daemon=True,
            name="chat-ui",
        )
        thread.start()

    def _run_command(self, text: str) -> None:
        replies: list[str] = []

        def speak_cb(reply: str) -> None:
            if reply and reply.strip():
                replies.append(reply.strip())

        try:
            import commands

            commands.execute(text, speak_cb, channel="cli")
        except Exception as exc:
            logging.warning("[Chat] Ошибка маршрутизатора: %s", exc)
            replies.append("Не удалось обработать сообщение.")

        self._finished.emit(replies)

    def _on_finished(self, replies: list) -> None:
        if replies:
            for reply in replies:
                self._append("Джарвис", reply)
        else:
            self._append("Джарвис", "Готово.", muted=True)
        self._set_busy(False)
