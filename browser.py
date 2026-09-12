import logging
import os
import shutil
import signal
import subprocess

from skills.text_utils import has_any_word

_DEVNULL = subprocess.DEVNULL
_CHROME_NAMES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
_CLOSE_VERBS = ("закрой", "закрыть", "выключи", "выруби", "убери")
_BROWSER_WORDS = ("браузер", "хром", "chrome", "chromium", "firefox", "фаерфокс")
_BROWSER_PATHS = (
    "/opt/google/chrome/chrome",
    "/opt/google/chrome/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/lib/chromium",
    "/usr/bin/chromium",
    "/usr/lib/firefox",
    "/usr/bin/firefox",
    "/opt/brave.com/brave",
    "/usr/bin/brave",
    "/opt/yandex/browser",
)
_SKIP_CMDLINE = (
    "cursor",
    "electron",
    "chrome_crashpad",
    "cursorsandbox",
    "code -",
    "vscode",
)
_BROWSER_WIN_RE = r"chrome|chromium|firefox|brave|yandex"


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


def is_close_browser_text(text: str) -> bool:
    """«закрой браузер» / «выруби хром» — не путать с закрытием плеера."""
    lowered = (text or "").lower()
    return has_any_word(lowered, _CLOSE_VERBS) and has_any_word(lowered, _BROWSER_WORDS)


def _browser_pids() -> list[int]:
    try:
        out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
    except Exception as exc:
        logging.debug("[Browser] ps: %s", exc)
        return []
    pids: list[int] = []
    for line in out.splitlines():
        raw = line.strip()
        if not raw:
            continue
        pid_s, _, args = raw.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        args_l = args.lower()
        if any(skip in args_l for skip in _SKIP_CMDLINE):
            continue
        if any(path in args for path in _BROWSER_PATHS):
            pids.append(pid)
    return pids


def _kill_browser_processes() -> int:
    killed = 0
    for pid in _browser_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            logging.debug("[Browser] kill %s: %s", pid, exc)
    return killed


def close_browser() -> bool:
    """Закрывает окна браузера, затем гасит его процессы. Cursor/Electron не трогает."""
    closed_windows = 0
    try:
        from window_control import close_matching_windows

        closed_windows = close_matching_windows(_BROWSER_WIN_RE)
    except Exception as exc:
        logging.debug("[Browser] окна: %s", exc)
    killed = _kill_browser_processes()
    logging.info("[Browser] закрыто окон=%s процессов=%s", closed_windows, killed)
    return closed_windows > 0 or killed > 0
