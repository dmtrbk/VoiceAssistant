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
    ("site_apps", "Сайты", "YouTube, TikTok и другие приложения из браузера"),
    ("audacious", "Музыка и радио", "Audacious, папки, плейлисты и поиск песен"),
    ("weather", "Погода", "Прогноз Open-Meteo"),
    ("stocks", "Биржа и портфель", "Котировки Мосбиржи и сводка счёта Т-Инвест. Сделки — тумблеры ниже"),
    ("timer", "Таймеры", "Отсчёт и оповещение"),
    ("calculator", "Калькулятор", "Счёт без облака"),
    ("games", "Игры и рандомайзер", "Больше-Меньше, кубики d6/d20, случайные числа"),
    ("jokes", "Анекдоты и факты", "Шутки, тосты, сказки"),
    ("web_search", "Поиск в интернете", "Яндекс / Google в браузере"),
    ("wikipedia", "Википедия", "Краткая справка: что такое, кто такой"),
    ("maps", "Карты", "Поиск мест и маршруты"),
    ("image_gen", "Картинки", "Рисует бесплатно и шлёт в Telegram"),
    ("security", "Охрана", "Камера и снимки в Telegram"),
    ("telegram", "Telegram", "Открыть или закрыть приложение"),
    ("pentagon", "Пентагон", "Шутка с зелёным кодом в терминале"),
)

OPTIONAL_IDS = {item[0] for item in OPTIONAL_SKILLS}

# Группы окна настроек. Навык без группы попадёт в «Другое».
SKILL_GROUPS = (
    ("Дом", ("home_assistant", "xiaomi_bulb", "security")),
    ("Медиа", ("audacious", "movie", "image_gen", "site_apps")),
    ("Сеть", ("web_search", "wikipedia", "maps", "telegram")),
    ("Сервисы", ("weather", "stocks", "timer", "calculator")),
    ("Разное", ("games", "jokes", "pentagon")),
)


def grouped_skills() -> list[tuple[str, list[tuple[str, str, str]]]]:
    catalog = {sid: (title, hint) for sid, title, hint in OPTIONAL_SKILLS}
    seen: set[str] = set()
    groups: list[tuple[str, list[tuple[str, str, str]]]] = []
    for name, ids in SKILL_GROUPS:
        rows = []
        for sid in ids:
            item = catalog.get(sid)
            if item is None:
                continue
            rows.append((sid, item[0], item[1]))
            seen.add(sid)
        if rows:
            groups.append((name, rows))
    leftover = [(sid, title, hint) for sid, title, hint in OPTIONAL_SKILLS if sid not in seen]
    if leftover:
        groups.append(("Другое", leftover))
    return groups


def ordered_skill_ids() -> list[str]:
    return [sid for _name, rows in grouped_skills() for sid, _title, _hint in rows]


AUTO_TRADE_KEY = "stocks_auto_trade"
VOICE_TRADE_KEY = "stocks_voice_trade"
GROQ_MODEL_KEY = "groq_model"
STT_MODE_KEY = "stt_mode"
PERSONA_PRESET_KEY = "persona_preset"
_CURSOR_COMM = frozenset({"cursor", "cursor-bin"})

# GUI-поток читает очередь и открывает окно. Голос только кладёт «open».
settings_events: queue.Queue[str] = queue.Queue()

_lock = threading.Lock()
_enabled: dict[str, bool] = {sid: True for sid, _title, _hint in OPTIONAL_SKILLS}
_auto_trade = False
_voice_trade = False
_groq_model = ""
_stt_mode = ""
_persona_preset = ""
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


def _env_voice_trade_default() -> bool:
    """Сделки из Джарвиса выключены, пока явно не включат."""
    raw = (os.getenv("TINKOFF_VOICE_TRADE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_groq_model_default() -> str:
    from skills.groq_client import normalize_groq_model

    return normalize_groq_model(os.getenv("GROQ_MODEL"))


STT_MODE_CHOICES = (
    ("hybrid", "Гибридное (Vosk + Groq Whisper Turbo)"),
    ("vosk", "Оффлайн (только Vosk)"),
)


def normalize_stt_mode(raw: str | None) -> str:
    clean = (raw or "").strip().lower()
    if clean in {"vosk", "offline", "local"}:
        return "vosk"
    return "hybrid"


def _env_stt_mode_default() -> str:
    return normalize_stt_mode(os.getenv("STT_MODE"))


def stt_mode_choices() -> list[tuple[str, str]]:
    return list(STT_MODE_CHOICES)


def _env_persona_preset_default() -> str:
    from skills.persona import normalize_persona_preset

    raw = os.getenv("PERSONA_PRESET") or os.getenv("JARVIS_PERSONA")
    return normalize_persona_preset(raw)


def is_cursor_running() -> bool:
    """Редактор Cursor открыт — сильную модель не жжём, пока его не закроют."""
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(os.path.join("/proc", name, "comm"), encoding="utf-8") as handle:
                    if handle.read().strip().lower() in _CURSOR_COMM:
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


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
        image_gen_skill,
        wikipedia_skill,
        site_apps_skill,
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
        "image_gen": image_gen_skill,
        "wikipedia": wikipedia_skill,
        "site_apps": site_apps_skill,
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
        snapshot[VOICE_TRADE_KEY] = bool(_voice_trade)
        snapshot[GROQ_MODEL_KEY] = _groq_model
        snapshot[STT_MODE_KEY] = _stt_mode
        snapshot[PERSONA_PRESET_KEY] = _persona_preset
    try:
        _write_json_atomic(CONFIG_PATH, snapshot)
    except Exception as exc:
        logging.error("[Настройки] Не удалось сохранить %s: %s", CONFIG_PATH, exc)


def reload_from_disk() -> dict[str, bool]:
    """Читает skills_enabled.json. Нет ключа — навык включён."""
    global _enabled, _auto_trade, _voice_trade, _groq_model, _stt_mode, _persona_preset
    flags = {sid: True for sid, _title, _hint in OPTIONAL_SKILLS}
    auto_trade = _env_auto_trade_default()
    voice_trade = _env_voice_trade_default()
    groq_model = _env_groq_model_default()
    stt_mode = _env_stt_mode_default()
    persona_preset = _env_persona_preset_default()
    # Ключи в JSON важнее пустых TINKOFF_* / GROQ_MODEL.
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
                if VOICE_TRADE_KEY in raw:
                    voice_trade = bool(raw[VOICE_TRADE_KEY])
                if GROQ_MODEL_KEY in raw:
                    from skills.groq_client import normalize_groq_model

                    groq_model = normalize_groq_model(str(raw[GROQ_MODEL_KEY] or ""))
                if STT_MODE_KEY in raw:
                    stt_mode = normalize_stt_mode(str(raw[STT_MODE_KEY] or ""))
                if PERSONA_PRESET_KEY in raw:
                    from skills.persona import normalize_persona_preset

                    persona_preset = normalize_persona_preset(str(raw[PERSONA_PRESET_KEY] or ""))
        except Exception as exc:
            logging.error("[Настройки] Не удалось прочитать %s: %s", CONFIG_PATH, exc)
    with _lock:
        _enabled = flags
        _auto_trade = auto_trade
        _voice_trade = voice_trade
        _groq_model = groq_model
        _stt_mode = stt_mode
        _persona_preset = persona_preset
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


def is_voice_trade_enabled() -> bool:
    """Заявки по команде Джарвиса («купи», «продай», «поторгуй»)."""
    _ensure_loaded()
    with _lock:
        return bool(_voice_trade)


def set_voice_trade(enabled: bool) -> None:
    """Тумблер сделок голосом: сразу на диск и в окружение процесса."""
    global _voice_trade
    _ensure_loaded()
    with _lock:
        _voice_trade = bool(enabled)
    os.environ["TINKOFF_VOICE_TRADE"] = "true" if enabled else "false"
    _persist()
    logging.info("[Настройки] Сделки голосом: %s", "вкл" if enabled else "выкл")


def get_groq_model() -> str:
    """Модель, выбранная в настройках или из GROQ_MODEL."""
    _ensure_loaded()
    with _lock:
        return _groq_model or _env_groq_model_default()


def set_groq_model(model_id: str) -> None:
    """Комбо модели диалога: сразу на диск и в окружение процесса."""
    global _groq_model
    from skills.groq_client import normalize_groq_model

    chosen = normalize_groq_model(model_id)
    _ensure_loaded()
    with _lock:
        _groq_model = chosen
    os.environ["GROQ_MODEL"] = chosen
    _persist()
    logging.info("[Настройки] Модель диалога: %s", chosen)


def get_stt_mode() -> str:
    """Режим распознавания речи: 'hybrid' или 'vosk'."""
    _ensure_loaded()
    with _lock:
        return _stt_mode or _env_stt_mode_default()


def set_stt_mode(mode: str) -> None:
    """Переключает режим распознавания: сразу на диск и в окружение."""
    global _stt_mode
    chosen = normalize_stt_mode(mode)
    _ensure_loaded()
    with _lock:
        _stt_mode = chosen
    os.environ["STT_MODE"] = chosen
    _persist()
    logging.info("[Настройки] Распознавание речи: %s", chosen)


def get_persona_preset() -> str:
    """Выбранный пресет характера Джарвиса ('jarvis', 'sarcastic', 'brutal', 'buddy', 'custom')."""
    _ensure_loaded()
    with _lock:
        return _persona_preset or _env_persona_preset_default()


def set_persona_preset(preset: str) -> None:
    """Переключает характер ассистента, сохраняет и обновляет промпт в памяти."""
    global _persona_preset
    from skills.persona import normalize_persona_preset

    chosen = normalize_persona_preset(preset)
    _ensure_loaded()
    with _lock:
        was = _persona_preset
        _persona_preset = chosen
    os.environ["PERSONA_PRESET"] = chosen
    _persist()
    if was != chosen:
        logging.info("[Настройки] Характер Джарвиса изменён: %s -> %s", was or "jarvis", chosen)
    try:
        from skills import ai_chat_skill

        if ai_chat_skill is not None:
            ai_chat_skill.reload_persona()
    except Exception as exc:
        logging.debug("[Настройки] reload_persona: %s", exc)


def persona_preset_choices() -> list[tuple[str, str]]:
    from skills.persona import persona_preset_choices as _choices

    return _choices()


def get_persona_hint(preset: str) -> str:
    from skills.persona import get_persona_hint as _hint

    return _hint(preset)


def is_online_stt_enabled() -> bool:
    """Включено ли онлайн/гибридное распознавание через Groq Whisper."""
    mode = get_stt_mode()
    has_key = bool((os.getenv("GROQ_API_KEY") or "").strip().strip("\"'"))
    return mode == "hybrid" and has_key


def get_effective_groq_model() -> str:
    """Сильная ждёт закрытия Cursor, чтобы не жечь квоту Groq во время правки кода."""
    from skills.groq_client import FAST_MODEL

    preferred = get_groq_model()
    if preferred != FAST_MODEL and is_cursor_running():
        return FAST_MODEL
    return preferred


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
