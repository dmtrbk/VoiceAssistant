# skill_settings.py
# Тумблеры необязательных навыков. Каркас (диалог, прощание, система, время)
# в окне не показывается и выключить нельзя.

import json
import logging
import os
import queue
import threading
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "skills_enabled.json")

# id, заголовок, подсказка. Порядок = порядок в окне.
OPTIONAL_SKILLS = (
    ("xiaomi_bulb", "Свет", "Лампа Xiaomi / Yeelight"),
    ("home_assistant", "Home Assistant", "Умный дом: свет, розетки, сцены по имени"),
    ("movie", "Фильмы и видео", "ВК Видео и плеер MPV"),
    ("audacious", "Музыка и радио", "Audacious, папки, плейлисты и поиск песен"),
    ("weather", "Погода", "Прогноз Open-Meteo"),
    ("stocks", "Биржа и портфель", "Котировки Мосбиржи, брокерский счёт Т-Инвест, сделки и авто-ребалансировка"),
    ("timer", "Таймеры", "Отсчёт и оповещение"),
    ("calculator", "Калькулятор", "Счёт без облака"),
    ("games", "Игры и рандомайзер", "Больше-Меньше, кубики d6/d20, случайные числа"),
    ("jokes", "Анекдоты и факты", "Шутки, тосты, сказки"),
    ("web_search", "Поиск в интернете", "Яндекс / Google в браузере"),
    ("maps", "Карты", "Поиск мест и маршруты"),
    ("security", "Охрана", "Камера и снимки в Telegram"),
    ("telegram", "Telegram", "Открыть или закрыть приложение"),
    ("pentagon", "Пентагон", "Шутка с зелёным кодом в терминале"),
)

OPTIONAL_IDS = {item[0] for item in OPTIONAL_SKILLS}
AUTO_TRADE_KEY = "stocks_auto_trade"

# GUI-поток читает очередь и открывает окно. Голос только кладёт «open».
settings_events: queue.Queue[str] = queue.Queue()

_lock = threading.Lock()
_enabled: dict[str, bool] = {sid: True for sid, _title, _hint in OPTIONAL_SKILLS}
_auto_trade = False
_loaded = False
_SKILL_MAP: dict[str, Any] | None = None


def _env_auto_trade_default() -> bool:
    """Как в stocks._auto_trade_enabled: явный флаг, иначе только песочница."""
    raw = (os.getenv("TINKOFF_AUTO_TRADE") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return (os.getenv("TINKOFF_SANDBOX") or "").strip().lower() in {"1", "true", "yes", "on"}


def _write_json_atomic(path: str, data: Any) -> None:
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def _skill_map() -> dict[str, Any]:
    global _SKILL_MAP
    if _SKILL_MAP is not None:
        return _SKILL_MAP
    from skills import (
        audacious_skill,
        calculator_skill,
        games_skill,
        jokes_skill,
        maps_search_skill,
        movie_skill,
        pentagon_skill,
        security_skill,
        telegram_skill,
        timer_skill,
        weather_skill,
        web_search_skill,
        xiaomi_bulb_skill,
        home_assistant_skill,
        stocks_skill,
    )

    _SKILL_MAP = {
        "xiaomi_bulb": xiaomi_bulb_skill,
        "movie": movie_skill,
        "audacious": audacious_skill,
        "weather": weather_skill,
        "timer": timer_skill,
        "calculator": calculator_skill,
        "games": games_skill,
        "jokes": jokes_skill,
        "web_search": web_search_skill,
        "maps": maps_search_skill,
        "security": security_skill,
        "telegram": telegram_skill,
        "pentagon": pentagon_skill,
        "home_assistant": home_assistant_skill,
        "stocks": stocks_skill,
    }
    return _SKILL_MAP


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    reload_from_disk()
    _loaded = True


def _persist() -> None:
    with _lock:
        snapshot = dict(_enabled)
        snapshot[AUTO_TRADE_KEY] = bool(_auto_trade)
    try:
        _write_json_atomic(CONFIG_PATH, snapshot)
    except Exception as exc:
        logging.error("[Настройки] Не удалось сохранить %s: %s", CONFIG_PATH, exc)


def reload_from_disk() -> dict[str, bool]:
    """Читает skills_enabled.json. Нет ключа — навык включён."""
    global _enabled, _auto_trade
    flags = {sid: True for sid, _title, _hint in OPTIONAL_SKILLS}
    auto_trade = _env_auto_trade_default()
    # Ключ stocks_auto_trade в JSON важнее пустого TINKOFF_AUTO_TRADE.
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                for sid in OPTIONAL_IDS:
                    if sid in raw:
                        flags[sid] = bool(raw[sid])
                if AUTO_TRADE_KEY in raw:
                    auto_trade = bool(raw[AUTO_TRADE_KEY])
        except Exception as exc:
            logging.error("[Настройки] Не удалось прочитать %s: %s", CONFIG_PATH, exc)
    with _lock:
        _enabled = flags
        _auto_trade = auto_trade
    return dict(flags)


def get_flags() -> dict[str, bool]:
    _ensure_loaded()
    with _lock:
        return dict(_enabled)


def skill_id_of(skill: Any) -> str | None:
    for sid, inst in _skill_map().items():
        if inst is skill:
            return sid
    from skills import music_search_skill
    if skill is music_search_skill:
        return "audacious"
    return None


def is_skill_enabled(skill: Any) -> bool:
    sid = skill_id_of(skill)
    if sid is None:
        return True
    _ensure_loaded()
    with _lock:
        return _enabled.get(sid, True)


def is_auto_trade_enabled() -> bool:
    """Фоновые заявки навыка «Биржа и портфель»."""
    _ensure_loaded()
    with _lock:
        return bool(_auto_trade)


def set_auto_trade(enabled: bool) -> None:
    """Тумблер автоторговли: сразу на диск и в окружение процесса."""
    global _auto_trade
    _ensure_loaded()
    with _lock:
        _auto_trade = bool(enabled)
    os.environ["TINKOFF_AUTO_TRADE"] = "true" if enabled else "false"
    _persist()
    if enabled:
        try:
            from skills import stocks_skill
            stocks_skill.start_background()
        except Exception as exc:
            logging.debug("[Настройки] Старт автоторговли: %s", exc)
    logging.info("[Настройки] Автоторговля: %s", "вкл" if enabled else "выкл")


def request_open_settings() -> None:
    settings_events.put("open")


def set_flag(skill_id: str, enabled: bool) -> None:
    """Включает или выключает один необязательный навык и сразу пишет файл."""
    if skill_id not in OPTIONAL_IDS:
        return
    _ensure_loaded()
    with _lock:
        was = _enabled.get(skill_id, True)
        _enabled[skill_id] = bool(enabled)
    _persist()
    if was and not enabled:
        _disable_runtime(skill_id)
    elif (not was) and enabled:
        _enable_runtime(skill_id)
    logging.info("[Настройки] Навык %s: %s", skill_id, "вкл" if enabled else "выкл")


def _disable_runtime(skill_id: str) -> None:
    inst = _skill_map().get(skill_id)
    if inst is None:
        return
    try:
        from commands import forget_skill
        forget_skill(inst)
        if skill_id == "audacious":
            from skills import music_search_skill
            forget_skill(music_search_skill)
    except Exception as exc:
        logging.debug("[Настройки] forget_skill: %s", exc)
    try:
        inst.on_disabled()
    except Exception as exc:
        logging.error("[Настройки] on_disabled у %s: %s", skill_id, exc)


def _enable_runtime(skill_id: str) -> None:
    inst = _skill_map().get(skill_id)
    if inst is None:
        return
    try:
        inst.on_enabled()
    except Exception as exc:
        logging.error("[Настройки] on_enabled у %s: %s", skill_id, exc)
