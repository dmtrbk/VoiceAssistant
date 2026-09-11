import logging
import os
import shutil
import subprocess

_DEVNULL = subprocess.DEVNULL
_CHROME_NAMES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def chrome_command() -> list[str]:
    """Путь к Chrome/Chromium: CHROME_PATH, затем типичные бинарники."""
    env_path = (os.getenv("CHROME_PATH") or "").strip()
    if env_path:
        return [env_path]
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return [found]
    bundled = "/opt/google/chrome/google-chrome"
    if os.path.isfile(bundled):
        return [bundled]
    return ["xdg-open"]


def open_url(url: str) -> None:
    """Открывает URL в Chrome или через xdg-open."""
    cmd = chrome_command()
    try:
        subprocess.Popen(cmd + [url], stdout=_DEVNULL, stderr=_DEVNULL)
    except FileNotFoundError:
        if cmd[0] != "xdg-open":
            try:
                subprocess.Popen(["xdg-open", url], stdout=_DEVNULL, stderr=_DEVNULL)
                return
            except Exception as exc:
                logging.error("[Browser] Не удалось открыть URL: %s", exc)
                return
        logging.error("[Browser] Не удалось открыть URL: браузер не найден")
