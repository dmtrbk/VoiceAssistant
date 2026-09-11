#!/usr/bin/env python3
# cli.py
"""
Консольный интерфейс (CLI) для взаимодействия с голосовым ассистентом Джарвис.
Поддерживает интерактивный диалог, разовые команды и голосовое воспроизведение ответов.
"""

import sys
import os
import argparse
import subprocess
import threading
import logging

# Загружаем переменные окружения
from dotenv import load_dotenv
load_dotenv()

# Настройка логирования для чистого вывода в консоли
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

import commands
from context_manager import is_in_context, get_active_context
from tts_cache import get_or_synthesize_wav
from indicator import status_queue, create_orb_gui
from telegram_listener import start_telegram_listener_thread

# Цветовое форматирование терминала ANSI
COLOR_BLUE = "\033[94m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_MAGENTA = "\033[95m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


def play_audio_feedback(text: str, mute: bool = False):
    """Синтезирует и проигрывает аудио ответа через Piper/paplay."""
    if mute or not text.strip():
        return
    try:
        wav_path, _ = get_or_synthesize_wav(text)
        if wav_path and os.path.exists(wav_path):
            subprocess.run(
                ["paplay", wav_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
    except Exception:
        pass


def execute_cli_command(user_text: str, mute: bool = False, verbose: bool = False) -> bool:
    """
    Выполняет команду через маршрутизатор commands.py и выводит ответ.
    Возвращает should_sleep (True если прощание).
    """
    responses: list[str] = []

    try:
        status_queue.put("thinking")
    except Exception:
        pass

    def speak_cb(text: str):
        if text and text.strip():
            responses.append(text.strip())
            try:
                status_queue.put("speaking")
            except Exception:
                pass
            print(f"{COLOR_CYAN}{COLOR_BOLD}[Джарвис]:{COLOR_RESET} {text}")
            if not mute:
                play_audio_feedback(text, mute=mute)

    should_sleep = commands.execute(user_text, speak_cb, channel="cli")

    try:
        if should_sleep:
            status_queue.put("idle")
        elif is_in_context():
            status_queue.put("listening")
        else:
            status_queue.put("idle")
    except Exception:
        pass

    return should_sleep


def run_interactive_loop(mute: bool = False, verbose: bool = False):
    """Запускает интерактивную командную строку."""
    start_telegram_listener_thread()
    print(f"\n{COLOR_MAGENTA}{COLOR_BOLD}======================================================{COLOR_RESET}")
    print(f"{COLOR_MAGENTA}{COLOR_BOLD}         🤖 ГОЛОСОВОЙ АССИСТЕНТ ДЖАРВИС — CLI         {COLOR_RESET}")
    print(f"{COLOR_MAGENTA}{COLOR_BOLD}======================================================{COLOR_RESET}")
    print(f"{COLOR_YELLOW}Режим озвучки:{COLOR_RESET} {'ВЫКЛЮЧЕН (--mute)' if mute else 'ВКЛЮЧЕН (Piper TTS)'}")
    print(f"{COLOR_YELLOW}Для выхода:{COLOR_RESET} введите '{COLOR_BOLD}exit{COLOR_RESET}', '{COLOR_BOLD}quit{COLOR_RESET}' или нажмите {COLOR_BOLD}Ctrl+C{COLOR_RESET}.\n")

    try:
        status_queue.put("idle")
    except Exception:
        pass

    while True:
        try:
            ctx = get_active_context()
            prompt_prefix = f"({ctx.name}) " if ctx else ""
            prompt_str = f"{COLOR_GREEN}{COLOR_BOLD}Вы {prompt_prefix}> {COLOR_RESET}"
            
            try:
                status_queue.put("listening" if ctx else "idle")
            except Exception:
                pass

            user_input = input(prompt_str).strip()
            if not user_input:
                continue

            if user_input.lower() in ["exit", "quit", "выход", "q"]:
                try:
                    status_queue.put("speaking")
                except Exception:
                    pass
                print(f"{COLOR_CYAN}[Джарвис]:{COLOR_RESET} До связи!")
                if not mute:
                    play_audio_feedback("До связи!", mute=mute)
                try:
                    status_queue.put("idle")
                except Exception:
                    pass
                break

            should_sleep = execute_cli_command(user_input, mute=mute, verbose=verbose)
            if should_sleep:
                print(f"\n{COLOR_YELLOW}[Сессия завершена]{COLOR_RESET}")

        except (KeyboardInterrupt, EOFError):
            print(f"\n{COLOR_CYAN}[Джарвис]:{COLOR_RESET} Завершение работы. До свидания!")
            try:
                status_queue.put("idle")
            except Exception:
                pass
            break


def run_cli_with_gui(mute: bool = False, verbose: bool = False):
    """Запускает графическую сферу на главном потоке и интерактивный CLI в фоновом потоке."""
    # Если дисплей не доступен — бесшовно запускаем чистый CLI
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        run_interactive_loop(mute=mute, verbose=verbose)
        return

    try:
        from PySide6.QtCore import QTimer
        import signal

        app, widget, worker, settings_worker = create_orb_gui()

        # При Ctrl+C выходим из Qt-цикла
        def sigint_handler(sig, frame):
            print(f"\n{COLOR_CYAN}[Джарвис]:{COLOR_RESET} Завершение работы. До свидания!")
            QTimer.singleShot(0, app.quit)

        signal.signal(signal.SIGINT, sigint_handler)

        def cli_worker():
            try:
                run_interactive_loop(mute=mute, verbose=verbose)
            finally:
                QTimer.singleShot(0, app.quit)

        cli_thread = threading.Thread(target=cli_worker, daemon=True)
        cli_thread.start()

        app.exec()
    except Exception as e:
        logging.warning(f"[CLI] Графический виджет не запущен ({e}), переключение в режим чистого терминала.")
        run_interactive_loop(mute=mute, verbose=verbose)


def main():
    parser = argparse.ArgumentParser(description="Консольный интерфейс ассистента Джарвис")
    parser.add_argument("query", nargs="*", help="Разовая команда (если не указана, откроется интерактивный режим)")
    parser.add_argument("-m", "--mute", "--no-voice", action="store_true", help="Отключить озвучку ответов через динамики")
    parser.add_argument("-d", "--debug", "-v", "--verbose", action="store_true", help="Подробный режим отладки (DEBUG logging)")
    parser.add_argument("--no-gui", "--headless", action="store_true", help="Запустить без графического виджета-сферы")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    env_no_gui = os.getenv("GUI_ENABLED", "true").lower() in ["0", "false", "no"]
    use_gui = not (args.no_gui or env_no_gui)

    if args.query:
        cmd_text = " ".join(args.query)
        execute_cli_command(cmd_text, mute=args.mute, verbose=args.debug)
    else:
        if use_gui:
            run_cli_with_gui(mute=args.mute, verbose=args.debug)
        else:
            run_interactive_loop(mute=args.mute, verbose=args.debug)


if __name__ == "__main__":
    main()
