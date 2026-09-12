# skills/security.py

import os
import re
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


def _camera_index() -> int:
    raw = (os.getenv("CAMERA_INDEX") or os.getenv("VIDEO_DEVICE") or "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        if raw.startswith("/dev/video"):
            suffix = raw[len("/dev/video"):]
            try:
                return max(0, int(suffix))
            except ValueError:
                return 0
        return 0

class SurveillanceThread(threading.Thread):
    """Поток для анализа изображения с веб-камеры."""
    def __init__(self, save_dir=None):
        super().__init__()
        self.daemon = True
        self._stop_event = threading.Event()
        if save_dir is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            save_dir = os.path.join(project_root, "surveillance_snaps")
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
        
        cap = cv2.VideoCapture(_camera_index())
        if not cap.isOpened():
            logging.error("[Охрана] Не удалось открыть веб-камеру (CAMERA_INDEX=%s).", _camera_index())
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        ret, frame1 = cap.read()
        if not ret:
            logging.error("[Охрана] Не удалось получить кадр с камеры.")
            cap.release()
            return

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

        stopped = self._stop_event.is_set()
        cap.release()
        logging.info("[Охрана] Поток видеонаблюдения остановлен.")
        if not stopped:
            send_telegram_notification("Камера охраны остановилась: нет кадра с веб-камеры.")


class SecuritySkill(BaseSkill):
    """Навык управления безопасностью помещения и заставками."""
    
    def __init__(self):
        self.surveillance_thread = None
        self.black_screen_process = None
        self._arm_lock = threading.Lock()
        self._arm_id = 0

    def _bump_arm(self) -> int:
        with self._arm_lock:
            self._arm_id += 1
            return self._arm_id

    def _stop_camera(self) -> None:
        if self.surveillance_thread is not None and self.surveillance_thread.is_alive():
            self.surveillance_thread.stop()
            self.surveillance_thread.join(timeout=5.0)
            if self.surveillance_thread.is_alive():
                logging.warning("[Охрана] Поток наблюдения не остановился за 5 секунд.")
            else:
                self.surveillance_thread = None

    def _start_camera(self) -> None:
        self._stop_camera()
        self.surveillance_thread = SurveillanceThread()
        self.surveillance_thread.start()

    def on_disabled(self) -> None:
        """Тихо гасит камеру и заставку, если охрану выключили тумблером."""
        self._bump_arm()
        try:
            self.control_screens(True)
        except Exception as exc:
            logging.debug("[Охрана] Не удалось убрать заставку: %s", exc)
        self._stop_camera()
        if self.surveillance_thread is None:
            logging.info("[Охрана] Наблюдение остановлено: навык выключен в настройках.")

    @staticmethod
    def _norm_security(text: str) -> str:
        cleaned = (text or "").lower().replace("ё", "е").strip()
        return re.sub(r"\s+", " ", cleaned)

    def _is_arm_command(self, text: str) -> bool:
        cleaned = self._norm_security(text)
        return any(
            phrase in cleaned
            for phrase in ("я ухожу", "включи охрану", "активируй охрану", "режим охраны")
        )

    def _is_disarm_command(self, text: str) -> bool:
        """«я тут» только целиком или хвостом, не внутри «я тут подумал»."""
        cleaned = self._norm_security(text)
        if any(
            phrase in cleaned
            for phrase in ("выключи охрану", "отключи охрану", "джарвис я тут")
        ):
            return True
        return bool(re.search(r"(?:^|\s)(?:ну\s+)?я\s+(?:тут|дома|пришел)$", cleaned))

    def can_handle(self, context: RequestContext) -> bool:
        text = context.raw_text
        if self._is_arm_command(text) or self._is_disarm_command(text):
            return True
        return False

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

        if self._is_arm_command(text):
            context.speak("Режим охраны активирован. Включаю заставку.")
            send_telegram_notification("🔒 Запущен режим охраны. Наблюдение начнется через 1 минуту.")
            arm_id = self._bump_arm()

            def _arm() -> None:
                time.sleep(2.5)
                with self._arm_lock:
                    if arm_id != self._arm_id:
                        return
                self.control_screens(False)
                self._start_camera()

            threading.Thread(target=_arm, daemon=True, name="security-arm").start()
            return

        if self._is_disarm_command(text):
            self._bump_arm()
            self.control_screens(True)
            self._stop_camera()
            context.speak("С возвращением! Система видеонаблюдения отключена.")
            send_telegram_notification("🔓 Режим охраны успешно отключен. Хозяин дома.")
            return
