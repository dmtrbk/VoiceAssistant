# skills/security.py

import os
import sys
import time
import logging
import threading
import subprocess
from skills.base import BaseSkill, RequestContext
from skills.utils import send_telegram_notification

try:
    import cv2
except ImportError:
    cv2 = None
    logging.warning("[Система] Библиотека opencv-python не найдена. Видеонаблюдение будет недоступно.")

class SurveillanceThread(threading.Thread):
    """Поток для анализа изображения с веб-камеры."""
    def __init__(self, save_dir="/home/real/VoiceAssistant/surveillance_snaps"):
        super().__init__()
        self.daemon = True
        self._stop_event = threading.Event()
        self.save_dir = os.path.expanduser(save_dir)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

    def stop(self):
        self._stop_event.set()

    def run(self):
        if cv2 is None:
            logging.error("[Охрана] Ошибка: OpenCV не установлен.")
            return

        logging.info("[Охрана] Активирован режим ожидания 60 секунд. Даем вам время уйти.")
        
        is_stopped = self._stop_event.wait(60)
        if is_stopped:
            logging.info("[Охрана] Наблюдение отменено до начала работы.")
            return

        logging.info("[Охрана] Поток видеонаблюдения запущен в штатный режим.")
        send_telegram_notification("⚠️ Камера видеонаблюдения переведена в активный режим охраны.")
        
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            logging.error("[Охрана] Не удалось открыть веб-камеру (/dev/video0).")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        ret, frame1 = cap.read()
        if ret:
            frame1_resized = cv2.resize(frame1, (320, 240))
            frame1_gray = cv2.cvtColor(frame1_resized, cv2.COLOR_BGR2GRAY)
            frame1_blur = cv2.GaussianBlur(frame1_gray, (5, 5), 0)

        last_analysis_time = 0.0
        analysis_interval = 1.5

        while not self._stop_event.is_set():
            ret, frame2 = cap.read()
            if not ret:
                break

            current_time = time.time()
            if current_time - last_analysis_time < analysis_interval:
                continue

            last_analysis_time = current_time

            frame2_resized = cv2.resize(frame2, (320, 240))
            frame2_gray = cv2.cvtColor(frame2_resized, cv2.COLOR_BGR2GRAY)
            frame2_blur = cv2.GaussianBlur(frame2_gray, (5, 5), 0)

            diff = cv2.absdiff(frame1_blur, frame2_blur)
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            dilated = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

            motion_detected = False
            for contour in contours:
                if cv2.contourArea(contour) < 1500:
                    continue
                motion_detected = True
                break

            if motion_detected:
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                filename = os.path.join(self.save_dir, f"motion_{timestamp}.jpg")
                
                cv2.imwrite(filename, frame2)
                logging.warning(f"[Охрана] Обнаружено движение! Снимок сохранен: {filename}")
                
                send_telegram_notification("🚨 Внимание! Замечено движение в комнате!", photo_path=filename)
                last_analysis_time = time.time() + 3.5

            frame1_blur = frame2_blur

        cap.release()
        logging.info("[Охрана] Поток видеонаблюдения остановлен.")


class SecuritySkill(BaseSkill):
    """Навык управления безопасностью помещения и заставками."""
    
    def __init__(self):
        self.surveillance_thread = None
        self.black_screen_process = None

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text
        triggers = [
            "я ухожу", "включи охрану", "активируй охрану", "режим охраны",
            "я пришел", "выключи охрану", "отключи охрану", "я дома", "ирина я тут"
        ]
        return any(w in text for w in triggers)

    def control_screens(self, turn_on: bool):
        """Управление черной заставкой на экранах."""
        try:
            if turn_on:
                if self.black_screen_process is not None:
                    self.black_screen_process.terminate()
                    self.black_screen_process = None
                    logging.info("[Система] Черная заставка отключена.")
            else:
                if self.black_screen_process is None:
                    self.black_screen_process = subprocess.Popen([
                        sys.executable, "-c",
                        "import tkinter as tk; r=tk.Tk(); r.overrideredirect(True); r.configure(bg='black'); w=r.winfo_vrootwidth(); h=r.winfo_vrootheight(); r.geometry(f'{w}x{h}+0+0'); r.config(cursor='none'); r.bind('<Escape>', lambda e: r.destroy()); r.mainloop()"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logging.info("[Система] Черная заставка активирована на всех экранах.")
        except Exception as e:
            logging.error(f"Ошибка управления заставкой: {e}")

    def execute(self, context: RequestContext) -> None:
        text = context.raw_text

        if any(w in text for w in ["я ухожу", "включи охрану", "активируй охрану", "режим охраны"]):
            context.speak("Режим охраны активирован. Включаю заставку.")
            send_telegram_notification("🔒 Запущен режим охраны. Наблюдение начнется через 1 минуту.")
            
            time.sleep(2.5)  # Даем договорить
            
            self.control_screens(False)
            
            if self.surveillance_thread is None or not self.surveillance_thread.is_alive():
                self.surveillance_thread = SurveillanceThread()
                self.surveillance_thread.start()
            return

        if any(w in text for w in ["я пришел", "выключи охрану", "отключи охрану", "я дома", "ирина я тут"]):
            self.control_screens(True)
            
            if self.surveillance_thread is not None and self.surveillance_thread.is_alive():
                self.surveillance_thread.stop()
                self.surveillance_thread.join()
                self.surveillance_thread = None
                
            context.speak("С возвращением! Система видеонаблюдения отключена.")
            send_telegram_notification("🔓 Режим охраны успешно отключен. Хозяин дома.")
            return
