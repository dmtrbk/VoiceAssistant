import os
# Принудительно запускаем Qt6 в режиме X11 (XWayland).
# Это открывает доступ к абсолютному позиционированию, закреплению поверх окон и перетаскиванию.
os.environ["QT_QPA_PLATFORM"] = "xcb"

import logging
import queue
import sys
import threading
import time
from PySide6.QtCore import QPoint, QSocketNotifier, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontInfo,
    QFontMetrics,
    QPainter,
    QRadialGradient,
    QGuiApplication,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from chat_ui import ChatWindow
from settings_ui import SettingsWindow
from skill_settings import settings_events

# Очередь для передачи статусов из основного скрипта ассистента
status_queue: queue.Queue = queue.Queue(maxsize=32)


def put_status(status: str) -> None:
    """Кладёт статус; в headless без читателя старые значения вытесняются."""
    try:
        status_queue.put_nowait(status)
        return
    except queue.Full:
        pass
    try:
        status_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        status_queue.put_nowait(status)
    except queue.Full:
        pass

# Настройки цветов для разных статусов (стиль Manjaro)
STATUS_COLORS = {
    "idle": "#7F8C8D",       # Серый (ожидание)
    "listening": "#2ECC71",  # Зеленый (запись / внимание)
    "thinking": "#E67E22",   # Оранжевый (обработка)
    "speaking": "#35BF5C"    # Ярко-зеленый Manjaro (озвучка)
}

# Шар сверху, две строки кэша рынков снизу. Свечение не заходит на цифры.
_ORB_SIDE = 150
_CAPTION_TOP = 99
_CAPTION_SAMPLE = "+12 345 ₽ / 123 456 ₽"

class OrbWidget(QWidget):
    """Сфера и две строки рынков под ней. Окно одно: цифры едут вместе с шаром."""
    def __init__(self):
        super().__init__()
        # X11BypassWindowManagerHint выводит окно в обход оконного менеджера Mutter (GNOME).
        # Благодаря этому окно автоматически закрепляется на всех рабочих столах, всегда
        # остается поверх других окон и мгновенно реагирует на перемещение мышкой.
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.X11BypassWindowManagerHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._form = ""
        self._caption_font = self._make_caption_font()
        self._sync_form()

        self.current_color = QColor(STATUS_COLORS["idle"])
        self.glow_radius = 20.0
        self.pulse_direction = 1
        self.state = "idle"
        self.drag_position = None
        self._dragged = False
        self.settings_window = None
        self.chat_window = None
        self._pnl = (None, None, None, None)
        self._pnl_at = 0.0
        self._refreshing = False
        self._refresh_not_before = 0.0
        self._read_pnl()

        # Пульс: ~25 fps в диалоге, в простое реже, чтобы не крутить Qt вхолостую.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_pulse)
        self.timer.start(200)

    def _make_caption_font(self) -> QFont:
        # В конфиге Conky стояла Candara; на этой машине её нет — Noto Sans.
        font = QFont("Candara")
        font.setPointSize(10)
        if QFontInfo(font).family().lower() != "candara":
            font = QFont("Noto Sans")
            font.setPointSize(10)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    def _sync_form(self) -> None:
        try:
            from skill_settings import get_widget_form

            form = get_widget_form()
        except Exception:
            form = "orb"
        if form == self._form:
            return
        self._form = form
        self._apply_metrics()
        self._place_corner()
        self.update()

    def _apply_metrics(self) -> None:
        if self._form == "plate":
            self._caption_font.setPointSize(12)
            metrics = QFontMetrics(self._caption_font)
            text_w = metrics.horizontalAdvance(_CAPTION_SAMPLE)
            width = 10 + 4 + 10 + text_w + 12
            height = 8 + metrics.height() * 2 + 8
            self.resize(width, height)
            return
        self._caption_font.setPointSize(10)
        metrics = QFontMetrics(self._caption_font)
        width = max(_ORB_SIDE, metrics.horizontalAdvance(_CAPTION_SAMPLE) + 16)
        self.resize(width, _ORB_SIDE)

    def _place_corner(self) -> None:
        screen = QGuiApplication.primaryScreen().geometry()
        if self._form == "plate":
            # Плашка целиком на экране, в том же левом нижнем углу.
            self.move(
                screen.x() + 8,
                screen.y() + screen.height() - self.height() - 8,
            )
            return
        # Сфера: квадрат 150×150 на −15 за левый край и на 15 px ниже низа.
        margin_x = -15
        margin_y = -15
        ox = (self.width() - _ORB_SIDE) // 2
        x = screen.x() + margin_x - ox
        y = screen.y() + screen.height() - _ORB_SIDE - margin_y
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._place_corner)

    def _read_pnl(self) -> None:
        try:
            import conky_markets

            day = conky_markets.read_pnl_cache() or (None, None)
            value = conky_markets.read_value_cache()
            nxt = (day[0], day[1], value[0], value[1])
        except Exception:
            return
        if nxt != self._pnl:
            self._pnl = nxt
            self.update()

    def _poll_markets(self) -> None:
        now = time.monotonic()
        if now - self._pnl_at < 1.0:
            return
        self._pnl_at = now
        self._read_pnl()
        self._kick_refresh()

    def _kick_refresh(self) -> None:
        if self._refreshing or time.monotonic() < self._refresh_not_before:
            return
        try:
            import conky_markets

            if conky_markets.cache_is_fresh():
                return
        except Exception:
            return
        self._refreshing = True
        threading.Thread(target=self._refresh_markets, name="orb-markets", daemon=True).start()

    def _refresh_markets(self) -> None:
        try:
            import conky_markets

            conky_markets.load_text()
        except Exception as exc:
            logging.warning("[Сфера] Не обновил цифры рынков: %s", exc)
            self._refresh_not_before = time.monotonic() + 30
        finally:
            self._refreshing = False

    def set_status(self, status):
        self.state = status
        hex_color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        self.current_color = QColor(hex_color)
        if status in ("listening", "speaking", "thinking"):
            self.timer.setInterval(40)
        else:
            self.timer.setInterval(200)
        self.update()

    def _drain_queues(self) -> None:
        """Статус и «открой настройки» — в GUI-потоке, без QThread на вечном get()."""
        while True:
            try:
                status = status_queue.get_nowait()
            except queue.Empty:
                break
            self.set_status(status)
        while True:
            try:
                event = settings_events.get_nowait()
            except queue.Empty:
                break
            if event == "open":
                self.open_settings()

    def update_pulse(self):
        """Логика изменения радиуса свечения в зависимости от статуса"""
        self._drain_queues()
        self._poll_markets()
        self._sync_form()
        if self.state in ["listening", "speaking"]:
            # Плавная размеренная пульсация
            self.glow_radius += self.pulse_direction * 0.4
            if self.glow_radius >= 28.0 or self.glow_radius <= 16.0:
                self.pulse_direction *= -1
            self.update()
        elif self.state == "thinking":
            # Быстрая пульсация во время работы нейросети
            self.glow_radius += self.pulse_direction * 1.0
            if self.glow_radius >= 32.0 or self.glow_radius <= 12.0:
                self.pulse_direction *= -1
            self.update()
        else:
            # Медленный возврат к базовому размеру в режиме ожидания (idle)
            if self.glow_radius != 20.0:
                self.glow_radius += 0.2 if self.glow_radius < 20.0 else -0.2
                self.update()

    def paintEvent(self, event):
        """Сфера с цифрами под шаром или плашка: черта состояния и те же строки."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if self._form == "plate":
            self._paint_plate(painter)
            painter.end()
            return

        side = _ORB_SIDE
        origin_x = (self.width() - side) // 2
        center = QPoint(origin_x + side // 2, side // 2)

        glow_gradient = QRadialGradient(center, side / 2)
        glow_color = QColor(self.current_color)
        glow_color.setAlpha(55)
        glow_gradient.setColorAt(0.0, glow_color)
        edge_color = QColor(self.current_color)
        edge_color.setAlpha(0)
        glow_gradient.setColorAt(self.glow_radius / 40.0, edge_color)

        painter.setBrush(glow_gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(0, 0, self.width(), side)

        painter.setBrush(QColor(self.current_color))
        painter.drawEllipse(center, 22, 22)
        self._paint_caption(painter)
        painter.end()

    def _paint_plate(self, painter: QPainter) -> None:
        import conky_markets

        painter.setFont(self._caption_font)
        metrics = painter.fontMetrics()
        rows = conky_markets.caption_rows(*self._pnl)
        pad_x, pad_y = 10, 8
        bar_w, gap = 4, 10
        text_h = metrics.height() * 2
        bar = (pad_x, pad_y, bar_w, text_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90))
        painter.drawRoundedRect(bar[0] + 1, bar[1] + 1, bar[2], bar[3], 2, 2)
        color = QColor(self.current_color)
        alpha = int(max(140, min(255, 150 + (self.glow_radius - 12) * 5)))
        color.setAlpha(alpha)
        painter.setBrush(color)
        painter.drawRoundedRect(*bar, 2, 2)

        text_x = pad_x + bar_w + gap
        baseline = pad_y + metrics.ascent()
        step = metrics.height()
        for row in rows:
            cursor = text_x
            for text, piece_color in row:
                self._draw_shadowed(painter, text, piece_color, cursor, baseline)
                cursor += metrics.horizontalAdvance(text)
            baseline += step

    def _paint_caption(self, painter: QPainter) -> None:
        import conky_markets

        stocks, crypto, stocks_life, crypto_life = self._pnl
        rows = conky_markets.caption_rows(stocks, crypto, stocks_life, crypto_life)
        painter.setFont(self._caption_font)
        metrics = painter.fontMetrics()
        baseline = _CAPTION_TOP + metrics.ascent()
        step = metrics.height() + metrics.leading()
        for row in rows:
            self._paint_caption_row(painter, metrics, row, baseline)
            baseline += step

    def _paint_caption_row(self, painter, metrics, pieces, baseline: int) -> None:
        day, slash, life = pieces
        slash_w = metrics.horizontalAdvance(slash[0])
        slash_x = int(self.width() / 2 - slash_w / 2)
        day_x = slash_x - metrics.horizontalAdvance(day[0])
        life_x = slash_x + slash_w
        self._draw_shadowed(painter, day[0], day[1], day_x, baseline)
        self._draw_shadowed(painter, slash[0], slash[1], slash_x, baseline)
        self._draw_shadowed(painter, life[0], life[1], life_x, baseline)

    def _draw_shadowed(self, painter, text: str, color: str, x: int, baseline: int) -> None:
        painter.setPen(QColor(0, 0, 0, 190))
        painter.drawText(x + 1, baseline + 1, text)
        painter.setPen(QColor(f"#{color}"))
        painter.drawText(x, baseline, text)

    def open_settings(self):
        if self.settings_window is None:
            self.settings_window = SettingsWindow()
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def open_chat(self):
        """Двойной клик: скрыть сферу, показать чат. Закрытие чата вернёт сферу."""
        if self.chat_window is None:
            self.chat_window = ChatWindow()
            self.chat_window.closed.connect(self._on_chat_closed)
        else:
            self.chat_window.prepare_reopen()
        self.hide()
        self.chat_window.show()
        self.chat_window.raise_()
        self.chat_window.activateWindow()
        self.chat_window.focus_input()

    def _on_chat_closed(self):
        self.show()

    def _quit_app(self):
        request_gui_quit()

    def _show_context_menu(self):
        menu = QMenu(self)
        settings_action = QAction("Настройки", menu)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(QCursor.pos())

    # --- ЛКМ: drag; двойной клик: чат; ПКМ: меню ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragged = False
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position is not None:
            pos = event.globalPosition().toPoint() - self.drag_position
            if (pos - self.pos()).manhattanLength() > 3:
                self._dragged = True
            self.move(pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._dragged:
            self.open_chat()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

def create_orb_gui():
    """Создает QApplication и сферу. Очереди читает таймер виджета."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    widget = OrbWidget()
    widget.show()
    widget._place_corner()
    return app, widget


def request_gui_quit() -> bool:
    """Просит Qt выйти из exec(). Для CLI; служба выходит через os._exit."""
    app = QApplication.instance()
    if app is None:
        return False
    QTimer.singleShot(0, app.quit)
    return True


def install_qt_signal_wakeup(app, on_signal) -> None:
    """SIGTERM во время app.exec() иначе не доходит до Python: Qt сидит в poll."""
    import signal as signal_mod
    import socket

    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    signal_mod.set_wakeup_fd(writer.fileno())
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)

    def _woke(*_args) -> None:
        try:
            while True:
                chunk = reader.recv(256)
                if not chunk:
                    break
        except BlockingIOError:
            pass
        on_signal()

    notifier.activated.connect(_woke)
    app._va_wakeup = (reader, writer, notifier)


def run_gui(on_signal=None):
    """Точка входа для графического интерфейса Qt6"""
    app, _widget = create_orb_gui()
    if on_signal is not None:
        install_qt_signal_wakeup(app, on_signal)
    sys.exit(app.exec())

