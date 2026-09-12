# window_control.py
# Окна GNOME: ярлык WM (uinput / xdotool), wmctrl для X11, AT-SPI для GTK.

import fcntl
import logging
import os
import re
import shutil
import struct
import subprocess
import time
from typing import List, Optional

from skills.text_utils import has_any_word as _has_any_word, has_word as _has_word, norm as _norm

logger = logging.getLogger(__name__)

_DEVNULL = subprocess.DEVNULL
_ATSPI_ROOT = "/org/a11y/atspi/accessible/root"
_WM_SCHEMA = "org.gnome.desktop.wm.keybindings"
_SHOW_DESKTOP_TEMP = "<Super><Alt>F12"

_SKIP_APP = (
    "gnome-shell",
    "ibus",
    "xdg-desktop-portal",
    "mutter-x11-frames",
    "assistant.py",
    "conky",
    "scratch-music",
)
_SKIP_TITLES = ("настройки джарвиса",)

_MOD_TO_KEY = {
    "super": "leftmeta",
    "meta": "leftmeta",
    "alt": "leftalt",
    "primary": "leftctrl",
    "control": "leftctrl",
    "ctrl": "leftctrl",
    "shift": "leftshift",
}
_KEY_CODES = {
    "leftmeta": 125,
    "leftalt": 56,
    "leftctrl": 29,
    "leftshift": 42,
    "q": 16,
    "d": 32,
    "w": 17,
    "f4": 62,
    "f10": 68,
    "f12": 88,
}
_XDOTOOL_MOD = {
    "leftmeta": "Super",
    "leftalt": "alt",
    "leftctrl": "ctrl",
    "leftshift": "shift",
}

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0


_WINDOW_PLURAL = ("окна", "окон", "окошки", "окошек")
_WINDOW_ONE = ("окно", "окошко")
_ALL_WORDS = ("все", "всего", "всем", "всее")


def is_window_command_text(text: str) -> bool:
    """Фраза про окна / рабочий стол — не отдавать кино и «голому» закрой."""
    lowered = _norm(text)
    if "рабочий стол" in lowered:
        return True
    return _has_any_word(lowered, _WINDOW_ONE + _WINDOW_PLURAL)


def detect_window_action(text: str) -> Optional[str]:
    """show_desktop | close_focused | close_all | None. Голое «закрой» не ловим."""
    lowered = _norm(text)
    if not lowered:
        return None

    wants_all = _has_any_word(lowered, _ALL_WORDS)
    if "рабочий стол" in lowered and _has_any_word(lowered, ("покажи", "открой", "сверни")):
        return "show_desktop"
    if _has_any_word(lowered, ("сверни", "свернуть")) and (
        wants_all or _has_any_word(lowered, _WINDOW_ONE + _WINDOW_PLURAL)
    ):
        return "show_desktop"

    close = _has_any_word(lowered, ("закрой", "закрыть", "убери"))
    if not close:
        return None
    # «закрой окна» / «закрой всего окна» (Vosk часто пишет «всего» вместо «все»)
    if _has_any_word(lowered, _WINDOW_PLURAL) or (
        wants_all and _has_any_word(lowered, _WINDOW_ONE)
    ):
        return "close_all"
    if _has_any_word(lowered, _WINDOW_ONE):
        return "close_focused"
    return None


def _gui_env() -> dict:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


def _atspi_env() -> dict:
    env = _gui_env()
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/at-spi/bus"
    return env


def _run(cmd: List[str], env: Optional[dict] = None, timeout: float = 2.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or _gui_env(),
    )


def _gsettings_get(key: str) -> str:
    try:
        res = _run(["gsettings", "get", _WM_SCHEMA, key])
        return (res.stdout or "").strip()
    except Exception as exc:
        logger.debug("[Окна] gsettings get %s: %s", key, exc)
        return ""


def _gsettings_set(key: str, value: str) -> bool:
    try:
        res = _run(["gsettings", "set", _WM_SCHEMA, key, value])
        return res.returncode == 0
    except Exception as exc:
        logger.debug("[Окна] gsettings set %s: %s", key, exc)
        return False


def parse_wm_binding(raw: str) -> Optional[List[str]]:
    """['<Super>q'] → ['leftmeta', 'q']."""
    if not raw or raw in ("@as []", "[]"):
        return None
    match = re.search(r"'([^']+)'", raw) or re.search(r'"([^"]+)"', raw)
    combo = match.group(1) if match else raw.strip()
    keys: List[str] = []
    for mod, key in re.findall(r"<([^>]+)>|([A-Za-z0-9_]+)", combo):
        token = (mod or key).lower()
        if token in _MOD_TO_KEY:
            keys.append(_MOD_TO_KEY[token])
        elif token in _KEY_CODES:
            keys.append(token)
        else:
            return None
    return keys or None


def _xdotool_combo(keys: List[str]) -> str:
    parts = []
    for key in keys:
        parts.append(_XDOTOOL_MOD.get(key, key if len(key) > 1 else key))
    return "+".join(parts)


def _uinput_emit(fd: int, ev_type: int, code: int, value: int) -> None:
    os.write(fd, struct.pack("@llHHi", 0, 0, ev_type, code, value))


def _keyboard_nodes() -> List[str]:
    """Реальные клавиатуры: виртуальный uinput GNOME часто игнорирует."""
    try:
        raw = open("/proc/bus/input/devices", encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    skip = ("button", "video bus", "speaker", "lid", "sleep", "power", "mouse", "touchpad", "avrcp")
    ranked: List[tuple] = []
    for block in raw.split("\n\n"):
        name_m = re.search(r'^N: Name="([^"]+)"', block, re.M)
        ev_m = re.search(r"^B: EV=(\S+)", block, re.M)
        hand_m = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if not (name_m and ev_m and hand_m):
            continue
        name = name_m.group(1)
        name_l = name.lower()
        if any(part in name_l for part in skip):
            continue
        if ev_m.group(1) != "120013":
            continue
        event = re.search(r"event(\d+)", hand_m.group(1))
        if not event:
            continue
        path = f"/dev/input/event{event.group(1)}"
        if not os.access(path, os.W_OK):
            continue
        score = 0 if "translated" in name_l else 1
        ranked.append((score, path))
    ranked.sort()
    return [path for _, path in ranked]


def _emit_key_events(fd: int, keys: List[str]) -> None:
    codes = [_KEY_CODES[k] for k in keys]
    for code in codes:
        _uinput_emit(fd, EV_KEY, code, 1)
        _uinput_emit(fd, EV_SYN, SYN_REPORT, 0)
    time.sleep(0.04)
    for code in reversed(codes):
        _uinput_emit(fd, EV_KEY, code, 0)
        _uinput_emit(fd, EV_SYN, SYN_REPORT, 0)


def _send_evdev(keys: List[str]) -> bool:
    if any(k not in _KEY_CODES for k in keys):
        return False
    for path in _keyboard_nodes():
        fd = None
        try:
            fd = os.open(path, os.O_WRONLY)
            _emit_key_events(fd, keys)
            logger.info("[Окна] Клавиши через %s: %s", path, "+".join(keys))
            return True
        except Exception as exc:
            logger.debug("[Окна] evdev %s: %s", path, exc)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return False


def _send_uinput(keys: List[str]) -> bool:
    path = "/dev/uinput"
    if not os.access(path, os.W_OK):
        return False
    if any(k not in _KEY_CODES for k in keys):
        return False
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in {_KEY_CODES[k] for k in keys}:
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        name = b"jarvis-wm".ljust(80, b"\0")
        os.write(fd, name + struct.pack("HHHH", 0x03, 0, 0, 1) + struct.pack("i", 0) + b"\0" * (64 * 16))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        time.sleep(0.12)
        _emit_key_events(fd, keys)
        time.sleep(0.05)
        fcntl.ioctl(fd, UI_DEV_DESTROY)
        return True
    except Exception as exc:
        logger.debug("[Окна] uinput: %s", exc)
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _send_xdotool(keys: List[str]) -> bool:
    if not shutil.which("xdotool"):
        return False
    combo = _xdotool_combo(keys)
    try:
        res = _run(["xdotool", "key", "--clearmodifiers", combo])
        return res.returncode == 0
    except Exception as exc:
        logger.debug("[Окна] xdotool: %s", exc)
        return False


def _send_keys(keys: List[str]) -> bool:
    if _send_evdev(keys):
        return True
    if _send_xdotool(keys):
        return True
    return _send_uinput(keys)


_EXT_DEST = "org.gnome.Shell"
_EXT_PATH = "/org/gnome/Shell/Extensions/JarvisWindows"
_EXT_IFACE = "org.gnome.Shell.Extensions.JarvisWindows"
_EXT_UUID = "jarvis-windows@voiceassistant"


def _extension_call(method: str, *args: str) -> Optional[str]:
    try:
        res = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                _EXT_DEST,
                "--object-path",
                _EXT_PATH,
                "--method",
                f"{_EXT_IFACE}.{method}",
                *args,
            ]
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception as exc:
        logger.debug("[Окна] extension %s: %s", method, exc)
    return None


def close_matching_windows(pattern: str) -> int:
    """Закрыть окна по wm_class/заголовку через расширение GNOME."""
    ensure_jarvis_windows_extension()
    ext = _extension_call("CloseMatching", pattern)
    if ext is None:
        return 0
    match = re.search(r"(-?\d+)", ext)
    closed = int(match.group(1)) if match else 0
    if closed:
        logger.info("[Окна] Закрыл %s окон по шаблону %s", closed, pattern)
    return max(closed, 0)


def ensure_jarvis_windows_extension() -> None:
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnome", _EXT_UUID)
    dst_dir = os.path.expanduser("~/.local/share/gnome-shell/extensions")
    dst = os.path.join(dst_dir, _EXT_UUID)
    if not os.path.isdir(src):
        return
    try:
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.islink(dst) or os.path.exists(dst):
            if os.path.realpath(dst) != os.path.realpath(src):
                if os.path.islink(dst):
                    os.remove(dst)
                    os.symlink(src, dst)
        else:
            os.symlink(src, dst)
    except OSError as exc:
        logger.debug("[Окна] Не удалось поставить расширение: %s", exc)
        return
    if _extension_call("Ping") is not None:
        return
    try:
        _run(["gnome-extensions", "enable", _EXT_UUID], timeout=3.0)
    except Exception as exc:
        logger.debug("[Окна] gnome-extensions enable: %s", exc)


def _close_shortcut() -> List[str]:
    return parse_wm_binding(_gsettings_get("close")) or ["leftmeta", "q"]


def _parse_atspi_refs(blob: str) -> List[tuple]:
    return re.findall(r"\('([^']+)', (?:objectpath )?'([^']+)'\)", blob or "")


def _gdbus(env: dict, dest: str, path: str, method: str, *args: str) -> str:
    cmd = ["gdbus", "call", "--session", "--dest", dest, "--object-path", path, "--method", method, *args]
    try:
        res = _run(cmd, env=env, timeout=2.5)
        return res.stdout or ""
    except Exception as exc:
        logger.debug("[Окна] gdbus %s: %s", method, exc)
        return ""


def _gdbus_prop(env: dict, dest: str, path: str, iface: str, prop: str) -> str:
    out = _gdbus(env, dest, path, "org.freedesktop.DBus.Properties.Get", iface, prop)
    match = re.search(r"<'([^']*)'>", out)
    if match:
        return match.group(1)
    match = re.search(r"<\"([^\"]*)\">", out)
    return match.group(1) if match else ""


def _is_protected(app: str, title: str, keep_mpv: bool) -> bool:
    app_l = (app or "").lower()
    title_l = (title or "").lower()
    if any(skip in app_l for skip in _SKIP_APP):
        return True
    if title_l in _SKIP_TITLES:
        return True
    if keep_mpv and ("mpv" in app_l or _has_word(title_l, "mpv")):
        return True
    return False


def _atspi_windows() -> List[dict]:
    env = _atspi_env()
    root_kids = _gdbus(env, "org.a11y.atspi.Registry", _ATSPI_ROOT, "org.a11y.atspi.Accessible.GetChildren")
    windows = []
    for dest, path in _parse_atspi_refs(root_kids):
        app = _gdbus_prop(env, dest, path, "org.a11y.atspi.Accessible", "Name")
        kids = _gdbus(env, dest, path, "org.a11y.atspi.Accessible.GetChildren")
        for child_dest, child_path in _parse_atspi_refs(kids):
            role = _gdbus(env, child_dest, child_path, "org.a11y.atspi.Accessible.GetRoleName")
            if "frame" not in role and "window" not in role:
                continue
            title = _gdbus_prop(env, child_dest, child_path, "org.a11y.atspi.Accessible", "Name")
            actions_blob = _gdbus(env, child_dest, child_path, "org.a11y.atspi.Action.GetActions")
            names = [m[0] for m in re.findall(r"\('([^']*)',\s*'([^']*)',\s*'([^']*)'\)", actions_blob)]
            windows.append(
                {
                    "dest": child_dest,
                    "path": child_path,
                    "app": app,
                    "title": title,
                    "actions": names,
                }
            )
    return windows


def _atspi_do(dest: str, path: str, action_name: str) -> bool:
    env = _atspi_env()
    blob = _gdbus(env, dest, path, "org.a11y.atspi.Action.GetActions")
    names = [m[0] for m in re.findall(r"\('([^']*)',\s*'([^']*)',\s*'([^']*)'\)", blob)]
    try:
        index = names.index(action_name)
    except ValueError:
        return False
    out = _gdbus(env, dest, path, "org.a11y.atspi.Action.DoAction", str(index))
    return "true" in out.lower()


def _atspi_focus(dest: str, path: str) -> bool:
    env = _atspi_env()
    out = _gdbus(env, dest, path, "org.a11y.atspi.Component.GrabFocus")
    return "true" in out.lower()


def _wmctrl_windows() -> List[dict]:
    if not shutil.which("wmctrl"):
        return []
    try:
        res = _run(["wmctrl", "-lx"])
    except Exception as exc:
        logger.debug("[Окна] wmctrl: %s", exc)
        return []
    windows = []
    for line in (res.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        win_id, desktop, klass = parts[0], parts[1], parts[2]
        title = parts[4] if len(parts) > 4 else ""
        windows.append(
            {
                "id": win_id,
                "desktop": desktop,
                "app": klass,
                "title": title,
            }
        )
    return windows


def _wmctrl_close(win_id: str) -> bool:
    try:
        res = _run(["wmctrl", "-ic", win_id])
        return res.returncode == 0
    except Exception:
        return False


def _wmctrl_minimize(win_id: str) -> bool:
    try:
        res = _run(["wmctrl", "-ir", win_id, "-b", "add,hidden"])
        return res.returncode == 0
    except Exception:
        return False


def _wmctrl_show_desktop() -> bool:
    if not shutil.which("wmctrl"):
        return False
    try:
        return _run(["wmctrl", "-k", "on"]).returncode == 0
    except Exception:
        return False


def _trigger_show_desktop_shortcut() -> bool:
    old = _gsettings_get("show-desktop")
    bound = parse_wm_binding(old)
    restore_needed = False
    try:
        if not bound:
            restore_needed = _gsettings_set("show-desktop", f"['{_SHOW_DESKTOP_TEMP}']")
            time.sleep(0.35)
            bound = parse_wm_binding(f"['{_SHOW_DESKTOP_TEMP}']")
        if not bound:
            return False
        if not _send_keys(bound):
            return False
        time.sleep(0.25)
        return True
    finally:
        if restore_needed:
            _gsettings_set("show-desktop", old if old else "[]")


def show_desktop() -> bool:
    """Свернуть все / показать рабочий стол."""
    ensure_jarvis_windows_extension()
    if _extension_call("ShowDesktop") is not None:
        logger.info("[Окна] Свернул через расширение GNOME Shell")
        return True

    if _trigger_show_desktop_shortcut():
        logger.info("[Окна] Свернул через ярлык show-desktop")
        return True

    ok = False
    for win in _atspi_windows():
        if _is_protected(win["app"], win["title"], keep_mpv=False):
            continue
        if "window.minimize" in win["actions"]:
            ok = _atspi_do(win["dest"], win["path"], "window.minimize") or ok
    for win in _wmctrl_windows():
        if win["desktop"] == "-1":
            continue
        if _is_protected(win["app"], win["title"], keep_mpv=False):
            continue
        ok = _wmctrl_minimize(win["id"]) or ok
    if ok:
        logger.info("[Окна] Свернул через AT-SPI/wmctrl")
    else:
        logger.warning("[Окна] Не удалось свернуть окна")
    return ok


def close_focused_window() -> bool:
    """Закрыть активное окно."""
    ensure_jarvis_windows_extension()
    if _extension_call("CloseFocused") is not None:
        logger.info("[Окна] Закрыл активное через расширение")
        return True
    return _send_keys(_close_shortcut())


def close_all_windows() -> int:
    """Закрыть обычные окна. Сферу, настройки и MPV не трогаем."""
    ensure_jarvis_windows_extension()
    ext = _extension_call("CloseAll")
    if ext is not None:
        match = re.search(r"(\d+)", ext)
        closed = int(match.group(1)) if match else 1
        logger.info("[Окна] Закрыл %s через расширение", closed)
        return closed

    closed = 0
    remaining = []
    for win in _atspi_windows():
        if _is_protected(win["app"], win["title"], keep_mpv=True):
            continue
        if "window.close" in win["actions"] and _atspi_do(win["dest"], win["path"], "window.close"):
            closed += 1
        else:
            remaining.append(win)
    for win in _wmctrl_windows():
        if win["desktop"] == "-1":
            continue
        if _is_protected(win["app"], win["title"], keep_mpv=True):
            continue
        if _wmctrl_close(win["id"]):
            closed += 1
    for win in remaining:
        if _is_protected(win["app"], win["title"], keep_mpv=True):
            continue
        if _atspi_focus(win["dest"], win["path"]) and _send_keys(_close_shortcut()):
            closed += 1
            time.sleep(0.2)
    return closed
