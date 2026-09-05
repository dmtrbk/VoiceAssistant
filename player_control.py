import os
import subprocess
import logging

_DEVNULL = subprocess.DEVNULL


def _popen(cmd: list[str]) -> None:
    try:
        subprocess.Popen(cmd, stdout=_DEVNULL, stderr=_DEVNULL)
    except FileNotFoundError as exc:
        logging.debug("[Player] Команда недоступна: %s", exc)


def start_player_session() -> bool:
    """Запускает пользовательский player_on.sh, если он есть."""
    script_on = os.path.expanduser("~/.scripts/player_on.sh")
    if os.path.exists(script_on):
        _popen(["systemd-run", "--user", "--scope", "bash", script_on])
        return True
    return False


def stop_player_session() -> None:
    """Останавливает Audacious/Glava через скрипт пользователя или точечный pkill."""
    script_off = os.path.expanduser("~/.scripts/player_off.sh")
    if os.path.exists(script_off):
        _popen(["systemd-run", "--user", "--scope", "bash", script_off])
        return

    _popen(["audtool", "--playback-stop"])
    _popen(["pkill", "-x", "audacious"])
    _popen(["pkill", "-x", "glava"])


def unload_loopback() -> None:
    _popen(["pactl", "unload-module", "module-loopback"])


def emergency_silence() -> None:
    """Срочная остановка медиа и loopback при команде «стоп»."""
    try:
        stop_player_session()
    except Exception as exc:
        logging.debug("[Player] Ошибка остановки плеера: %s", exc)
    try:
        unload_loopback()
    except Exception as exc:
        logging.debug("[Player] Ошибка выгрузки loopback: %s", exc)
