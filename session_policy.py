# session_policy.py
# Правила сессии внимания без микрофона, Vosk и Piper.
# assistant.py вызывает эти функции; тесты проверяют их напрямую.


def attention_timeout_sec(
    *,
    media_on: bool,
    has_pending: bool,
    timeout: float = 12.0,
    timeout_music: float = 5.0,
) -> float:
    """Секунды до сна: при медиа короче, пока ждём слот — не короче обычного."""
    current = timeout_music if media_on else timeout
    if has_pending:
        current = max(current, timeout)
    return current


def should_expire_attention(
    *,
    is_active: bool,
    is_speaking: bool,
    is_thinking: bool,
    in_context: bool,
    idle_for: float,
    timeout_sec: float,
) -> bool:
    """Пора ли уходить в idle. Поток timeout_monitor только вызывает это."""
    if not is_active or is_speaking or is_thinking or in_context:
        return False
    return idle_for > timeout_sec


def is_speaker_leak(
    phrase_rms: float,
    has_wake: bool,
    *,
    movie_playing: bool,
    session_active: bool,
    is_movie_control: bool,
    min_speech_rms: float,
    ducked_or_playing: bool,
) -> bool:
    """Речь с колонок: тихая песня или громкий фильм без имени активации."""
    if has_wake:
        return False
    if movie_playing:
        if session_active and is_movie_control:
            return False
        return True
    if min_speech_rms <= 0:
        return False
    if not ducked_or_playing:
        return False
    return phrase_rms < min_speech_rms


def voice_stop_goes_to_skill(in_context: bool) -> bool:
    """Голое «стоп» во время игры/FSM — в навык, не аварийное глушение медиа."""
    return bool(in_context)
