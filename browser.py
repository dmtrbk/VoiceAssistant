import logging
import subprocess

_DEVNULL = subprocess.DEVNULL


def open_url(url: str) -> None:
    """Открывает URL в Chrome или через xdg-open."""
    try:
        subprocess.Popen(["google-chrome-stable", url], stdout=_DEVNULL, stderr=_DEVNULL)
    except FileNotFoundError:
        try:
            subprocess.Popen(["xdg-open", url], stdout=_DEVNULL, stderr=_DEVNULL)
        except Exception as exc:
            logging.error("[Browser] Не удалось открыть URL: %s", exc)
