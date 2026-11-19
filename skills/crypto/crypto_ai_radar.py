# skills/crypto/crypto_ai_radar.py
# Фоновый AI-радар кандидатов щупа. Не берёт _TRADE_LOCK, Fail-Open при сбое LLM.
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from skills.groq_client import chat_kwargs, get_client

logger = logging.getLogger(__name__)

# TTL > circuit: иначе после паузы 240с кэш 180с уже мёртв и снова 15×TPM.
_AI_CACHE_TTL_SEC = 420.0
_AI_MIN_PASS_SCORE = 55
_AI_LOOP_SEC = 120.0
_AI_TOP_N = 15
# Пауза между тикерами + лимит за цикл: не выжигать минутный TPM пачкой.
_AI_GAP_SEC = 3.0
_AI_MAX_EVAL_PER_LOOP = 5
_AI_MISS_STREAK_LIMIT = 3
_AI_COOLDOWN_SEC = 240.0
_AI_TPM_COOLDOWN_SEC = 35.0
_AI_WARN_THROTTLE_SEC = 45.0
# Запас под reason/скобки: 48 иногда резал JSON → miss; короткий ответ всё равно ~25–35 tok.
_AI_MAX_TOKENS = 96
# Только qwen: gpt-oss + json_object + max_tokens=120 → 400 Failed to generate JSON.
_AI_MODELS = ("qwen/qwen3.8-27b",)
_SKIP_TICKERS = frozenset({"BTC", "ETH", "SOL", "USDT", "USDC", "USDE", "DAI"})


def _tpm_retry_sec(exc: BaseException) -> float | None:
    """Секунды паузы при 429 TPM/RPM; None — не rate-limit."""
    text = str(exc or "")
    low = text.lower()
    if "429" not in low and "rate limit" not in low and "tokens per" not in low:
        return None
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s", low)
    if match:
        return max(8.0, min(90.0, float(match.group(1)) + 1.5))
    return float(_AI_TPM_COOLDOWN_SEC)


def radar_enabled() -> bool:
    """Тумблер настроек + CRYPTO_AI_RADAR. Env=0 всегда гасит; иначе смотрим ai_radar_on."""
    raw = (os.getenv("CRYPTO_AI_RADAR") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    try:
        from .desk_policy import cfg

        return bool(cfg().ai_radar_on)
    except Exception:
        return False


@dataclass
class AIVerdict:
    ticker: str
    pass_trade: bool
    score: int
    veto_reason: str
    eval_ts: float


class CryptoAIRadar:
    """Демон: оценивает топ кандидатов вне _TRADE_LOCK; check_veto — мгновенный lookup."""

    def __init__(self, desk_skill: Any | None = None) -> None:
        self._skill = desk_skill
        self._cache: dict[str, AIVerdict] = {}
        self._candidates: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "pass": 0,
            "veto": 0,
            "miss": 0,
            "timeout": 0,
            "eval": 0,
        }
        self._miss_streak = 0
        self._cooldown_until = 0.0
        self._warn_ts: dict[str, float] = {}

    def start(self) -> None:
        if not radar_enabled():
            logger.info("[AI-Радар] выключен (настройки / CRYPTO_AI_RADAR)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._radar_loop,
            name="crypto-ai-radar",
            daemon=True,
        )
        self._thread.start()
        logger.info("[AI-Радар] фоновый поток на %s", _AI_MODELS[0])

    def stop(self) -> None:
        self._stop.set()

    def sync_enabled(self) -> None:
        """Подогнать поток под тумблер без рестарта всего стола."""
        if radar_enabled():
            self.start()
            return
        if self._thread and self._thread.is_alive():
            self.stop()
            logger.info("[AI-Радар] остановлен тумблером")

    def _warn_throttled(self, key: str, msg: str, *args: Any) -> None:
        now = time.time()
        last = float(self._warn_ts.get(key) or 0.0)
        if now - last < _AI_WARN_THROTTLE_SEC:
            return
        self._warn_ts[key] = now
        logger.warning(msg, *args)

    def offer_candidates(self, rows: list[dict[str, Any]] | None) -> None:
        """Снимок топа из скана щупа (без REST). Копия под замком радара."""
        if not radar_enabled():
            return
        try:
            from .desk_policy import SLEEVE_SKIP, probe_scan_day_ok
        except Exception:
            SLEEVE_SKIP = frozenset()  # type: ignore[assignment]
            probe_scan_day_ok = None  # type: ignore[assignment]
        clean: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper()
            if not ticker or ticker in _SKIP_TICKERS or ticker in SLEEVE_SKIP:
                continue
            price = float(row.get("price") or 0)
            day_chg = float(row.get("chg") or row.get("day_chg") or 0)
            high = float(row.get("high") or 0)
            # Не гоняем LLM на уже отсечённых (≥45% суток / глубокий слив с хая).
            if callable(probe_scan_day_ok) and not probe_scan_day_ok(
                price=price, day_high=high, day_chg=day_chg
            ):
                continue
            clean.append(
                {
                    "ticker": ticker,
                    "price": price,
                    "day_chg": day_chg,
                    "turnover_usd": float(row.get("turnover") or row.get("turnover_usd") or 0),
                    "high": high,
                    "low": float(row.get("low") or 0),
                }
            )
            if len(clean) >= _AI_TOP_N:
                break
        with self._lock:
            self._candidates = clean

    def check_veto(self, ticker: str) -> str | None:
        """Lookup для _TRADE_LOCK. None — Fail-Open / pass."""
        name = str(ticker or "").upper()
        if not name or not radar_enabled():
            return None
        now = time.time()
        with self._lock:
            verdict = self._cache.get(name)
        if not verdict or (now - float(verdict.eval_ts)) > _AI_CACHE_TTL_SEC:
            return None
        if verdict.pass_trade and int(verdict.score) >= _AI_MIN_PASS_SCORE:
            return None
        reason = str(verdict.veto_reason or "вето").strip() or "вето"
        return f"{reason} (AI {int(verdict.score)}/100)"

    def status_bit(self) -> str:
        with self._lock:
            n = len(self._cache)
            stats = dict(self._stats)
        if not radar_enabled():
            return "ИИ-радар: выкл"
        if get_client() is None:
            return "ИИ-радар: нет ключа"
        return (
            f"ИИ-радар: кэш {n}, "
            f"pass={stats['pass']}/veto={stats['veto']}/"
            f"miss={stats['miss']}/to={stats['timeout']}"
        )

    def _radar_loop(self) -> None:
        while not self._stop.is_set():
            if not radar_enabled():
                if self._stop.wait(_AI_LOOP_SEC):
                    break
                continue
            try:
                self._evaluate_top()
            except Exception as exc:
                logger.warning("[AI-Радар] цикл: %s", exc)
            if self._stop.wait(_AI_LOOP_SEC):
                break

    def _evaluate_top(self) -> None:
        if not radar_enabled() or get_client() is None:
            return
        now = time.time()
        if now < float(self._cooldown_until or 0.0):
            return
        with self._lock:
            rows = list(self._candidates)
        if not rows:
            return
        evaluated = 0
        for metrics in rows:
            if self._stop.is_set():
                break
            if time.time() < float(self._cooldown_until or 0.0):
                break
            if evaluated >= int(_AI_MAX_EVAL_PER_LOOP):
                break
            ticker = str(metrics.get("ticker") or "").upper()
            if not ticker:
                continue
            with self._lock:
                existing = self._cache.get(ticker)
                if existing and (now - float(existing.eval_ts)) < _AI_CACHE_TTL_SEC * 0.7:
                    continue
            verdict = self._call_llm(metrics)
            evaluated += 1
            if verdict is None:
                # TPM 429 уже выставил короткий cooldown — не жжём ещё 2 запроса → circuit 240с.
                if time.time() < float(self._cooldown_until or 0.0):
                    break
                self._miss_streak += 1
                if self._miss_streak >= _AI_MISS_STREAK_LIMIT:
                    self._cooldown_until = time.time() + _AI_COOLDOWN_SEC
                    self._warn_throttled(
                        "circuit",
                        "[AI-Радар] circuit: %d miss подряд — пауза %.0fс",
                        self._miss_streak,
                        _AI_COOLDOWN_SEC,
                    )
                    break
                if self._stop.wait(_AI_GAP_SEC):
                    break
                continue
            self._miss_streak = 0
            with self._lock:
                self._cache[ticker] = verdict
                self._stats["eval"] += 1
            if self._stop.wait(_AI_GAP_SEC):
                break

    def _call_llm(self, metrics: dict[str, Any]) -> AIVerdict | None:
        ticker = str(metrics.get("ticker") or "").upper()
        high = float(metrics.get("high") or 0)
        price = float(metrics.get("price") or 0)
        dist = 0.0
        if high > 1e-12 and price > 0:
            dist = (high - price) / high * 100.0
        # Без robotics_laws / длинного RU: иначе ~1.5–2k tok/монета → 429 TPM на 3-й.
        system = (
            "Crypto spot probe risk check. JSON only.\n"
            "Veto only: day>45% with turnover<1.5M, one-candle pump, or scam/delist.\n"
            "Else pass. Format: {\"pass_trade\":true,\"score\":85,\"reason\":\"OK\"}"
        )

        user = json.dumps(
            {
                "ticker": ticker,
                "day_change_pct": round(float(metrics.get("day_chg") or 0), 2),
                "turnover_usd": round(float(metrics.get("turnover_usd") or 0), 0),
                "current_price": price,
                "distance_to_high_pct": round(dist, 2),
            },
            ensure_ascii=False,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            started = time.time()
            raw = self._complete_json(messages)
            elapsed_ms = (time.time() - started) * 1000.0
            data = _parse_json_object(raw)
            pass_trade = bool(data.get("pass_trade", True))
            try:
                score = int(data.get("score", 70))
            except (TypeError, ValueError):
                score = 70
            reason = str(data.get("reason") or ("OK" if pass_trade else "вето")).strip()
            with self._lock:
                if pass_trade and score >= _AI_MIN_PASS_SCORE:
                    self._stats["pass"] += 1
                else:
                    self._stats["veto"] += 1
                    logger.info(
                        "[AI-Радар] VETO %s (%.0fмс): score=%d, %s",
                        ticker,
                        elapsed_ms,
                        score,
                        reason,
                    )
            return AIVerdict(
                ticker=ticker,
                pass_trade=pass_trade,
                score=score,
                veto_reason=reason,
                eval_ts=time.time(),
            )
        except Exception as exc:
            err = str(exc).lower()
            tpm_wait = _tpm_retry_sec(exc)
            with self._lock:
                if tpm_wait is not None:
                    self._stats["miss"] += 1
                    self._cooldown_until = max(
                        float(self._cooldown_until or 0.0),
                        time.time() + float(tpm_wait),
                    )
                    self._warn_throttled(
                        "tpm",
                        "[AI-Радар] TPM/RPM 429 на %s — пауза %.0fс (без circuit 240с)",
                        ticker,
                        tpm_wait,
                    )
                elif "timeout" in err or "timed out" in err:
                    self._stats["timeout"] += 1
                    self._warn_throttled(
                        f"to:{ticker}",
                        "[AI-Радар] таймаут Groq для %s: %s",
                        ticker,
                        exc,
                    )
                else:
                    self._stats["miss"] += 1
                    # Короткая выжимка причины (json_validate / 404) — раньше уходила в debug.
                    snippet = str(exc).replace("\n", " ").strip()
                    if len(snippet) > 180:
                        snippet = snippet[:177] + "..."
                    self._warn_throttled(
                        f"miss:{snippet[:48]}",
                        "[AI-Радар] сбой %s: %s",
                        ticker,
                        snippet,
                    )
            return None

    def _complete_json(self, messages: list[dict[str, str]]) -> str:
        client = get_client()
        if client is None:
            raise RuntimeError("Groq client unavailable")
        last_err: Exception | None = None
        for model_name in _AI_MODELS:
            try:
                kwargs = chat_kwargs(
                    messages,
                    model_name,
                    temperature=0.1,
                    max_tokens=_AI_MAX_TOKENS,
                )
                kwargs["response_format"] = {"type": "json_object"}
                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or "{}"
            except Exception as exc:
                last_err = exc
                from skills.groq_client import is_retriable_model_error

                if is_retriable_model_error(exc):
                    snippet = str(exc).replace("\n", " ").strip()
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    self._warn_throttled(
                        f"model:{model_name}",
                        "[AI-Радар] модель %s недоступна (%s)",
                        model_name,
                        snippet,
                    )
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("No response from Groq")


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
        raise
