"""JSONL-журналы paper/shadow + дневной снимок."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import LinearConfig

logger = logging.getLogger(__name__)


class LinearJournals:
    def __init__(self, cfg: LinearConfig, *, prefix: str = "paper") -> None:
        self.cfg = cfg
        self.prefix = prefix
        cfg.runtime_dir.mkdir(parents=True, exist_ok=True)

    def _append(self, path: Path, obj: dict[str, Any]) -> None:
        obj = dict(obj)
        obj.setdefault("ts_utc", datetime.now(timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def signal(self, row: dict[str, Any], *, shadow: bool = False) -> None:
        name = "shadow_aave_signals.jsonl" if shadow else f"{self.prefix}_signals.jsonl"
        self._append(self.cfg.path(name), row)

    def trade(self, row: dict[str, Any], *, shadow: bool = False) -> None:
        name = "shadow_aave_trades.jsonl" if shadow else f"{self.prefix}_trades.jsonl"
        self._append(self.cfg.path(name), row)

    def event(self, row: dict[str, Any]) -> None:
        self._append(self.cfg.path(f"{self.prefix}_events.jsonl"), row)

    def session_start(self, *, equity: float, extra: dict[str, Any] | None = None) -> None:
        """Маркер старта сессии — чтобы потом найти кусок прогона в jsonl."""
        row: dict[str, Any] = {
            "type": "session_start",
            "paper": bool(self.cfg.paper),
            "equity": float(equity),
            "symbols": list(self.cfg.symbols),
            "shadow": bool(self.cfg.shadow_enabled),
            "sizing_mode": self.cfg.sizing_mode,
            "leg_usd": self.cfg.leg_usd,
            "kill_daily_loss_usd": self.cfg.kill_daily_loss_usd,
            "kill_equity_floor_usd": self.cfg.kill_equity_floor_usd,
            "runtime_dir": str(self.cfg.runtime_dir),
        }
        if extra:
            row.update(extra)
        self.event(row)

    def daily_snapshot(self, *, equity: float, day_pnl: float, open_n: int, kill: bool) -> None:
        self._append(
            self.cfg.path(f"{self.prefix}_daily.jsonl"),
            {
                "type": "daily_snap",
                "equity": float(equity),
                "day_realized_pnl": float(day_pnl),
                "open_plus_pending": int(open_n),
                "kill": bool(kill),
            },
        )

    def read_signals_since(self, since_ms: int, *, shadow: bool = False) -> list[dict[str, Any]]:
        name = "shadow_aave_signals.jsonl" if shadow else f"{self.prefix}_signals.jsonl"
        path = self.cfg.path(name)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(obj.get("bar_ts") or 0) >= since_ms:
                out.append(obj)
        return out
