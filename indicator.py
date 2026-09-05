import os
# Принудительно запускаем Qt6 в режиме X11 (XWayland).
# Это открывает доступ к абсолютному позиционированию, закреплению поверх окон и перетаскиванию.
os.environ["QT_QPA_PLATFORM"] = "xcb"

import queue
import sys
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QRadialGradient, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

# Очередь для передачи статусов из основного скрипта ассистента
status_queue = queue.Queue()

# Настройки цветов для разных статусов в фирменном стиле Алисы (Яндекс Purple)
STATUS_COLORS = {
    "idle": "#6B657B",       # Приглушенный лавандово-серый (ожидание)
    "listening": "#8742FF",  # Фирменный фиолетовый Алисы (запись / внимание)
    "thinking": "#E056FD",   # Неоновый пурпурный (обработка нейросети)
    "speaking": "#A55EEA"    # Яркий фиолетовый градиент (озвучка)
}

class StatusWorker(QThread):
    """Фоновый поток для безопасного чтения очереди и передачи сигналов в GUI"""
    status_changed = Signal(str)

    def run(self):
        while True:
            try:
                status = status_queue.get()
                self.status_changed.emit(status)
                status_queue.task_done()
            except Exception:
                break

class OrbWidget(QWidget):
    """Интерактивный виджет сферы на рабочем столе"""
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
        self.resize(150, 150)
        
        # --- Позиционирование в левый нижний угол экрана ---
        screen = QGuiApplication.primaryScreen().geometry()
        
        margin_x = -15  # Отступ от левого края экрана
        margin_y = -15  # Отступ от нижнего края (с запасом под системную панель GNOME)
        
        x_pos = margin_x
        y_pos = screen.height() - self.height() - margin_y
        
        self.move(x_pos, y_pos)
        
        self.current_color = QColor(STATUS_COLORS["idle"])
        self.glow_radius = 20.0
        self.pulse_direction = 1
        self.state = "idle"
        self.drag_position = None
        
        # Таймер для анимации пульсации (~25 кадров в секунду)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_pulse)
        self.timer.start(40)

    def set_status(self, status):
        self.state = status
        hex_color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        self.current_color = QColor(hex_color)
        self.update()

    def update_pulse(self):
        """Логика изменения радиуса свечения в зависимости от статуса"""
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
        """Отрисовка сферы с мягким радиальным градиентом"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.rect()
        center = rect.center()
        
        # 1. Мягкое неоновое свечение (градиент)
        glow_gradient = QRadialGradient(center, rect.width() / 2)
        
        glow_color = QColor(self.current_color)
        glow_color.setAlpha(55)  # Полупрозрачность свечения
        glow_gradient.setColorAt(0.0, glow_color)
        
        edge_color = QColor(self.current_color)
        edge_color.setAlpha(0)   # Прозрачный край
        glow_gradient.setColorAt(self.glow_radius / 40.0, edge_color)
        
        painter.setBrush(glow_gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(rect)
        
        # 2. Плотное центральное ядро
        core_color = QColor(self.current_color)
        painter.setBrush(core_color)
        painter.drawEllipse(center, 22, 22)
        
        painter.end()

    # --- Перетаскивание виджета при зажатой левой кнопке мыши ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position is not None:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

def run_gui():
    """Точка входа для графического интерфейса Qt6"""
    app = QApplication(sys.argv)
    widget = OrbWidget()
    widget.show()
    
    # Запуск фонового отслеживания статусов из очереди
    worker = StatusWorker()
    worker.status_changed.connect(widget.set_status)
    worker.start()
    
    sys.exit(app.exec())
