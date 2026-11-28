"""Атомарный persist paper book (Core / Shadow раздельно)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .strategy_core import Signal

logger = logging.getLogger(__name__)


def core_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "paper_book_core.json"


def shadow_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "paper_book_shadow.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def _signal_to_dict(sig: Signal) -> dict[str, Any]:
    return {
        "symbol": sig.symbol,
        "side": sig.side,
        "bar_ts": int(sig.bar_ts),
        "limit_px": float(sig.limit_px),
        "sl": float(sig.sl),
        "tp": float(sig.tp),
        "risk_px": float(sig.risk_px),
        "atr15": float(sig.atr15),
        "er_4h": float(sig.er_4h),
        "confirm_close": float(sig.confirm_close),
    }


def _signal_from_dict(raw: dict[str, Any]) -> Signal:
    return Signal(
        symbol=str(raw["symbol"]).upper(),
        side=str(raw["side"]),
        bar_ts=int(raw["bar_ts"]),
        limit_px=float(raw["limit_px"]),
        sl=float(raw["sl"]),
        tp=float(raw["tp"]),
        risk_px=float(raw.get("risk_px") or 0),
        atr15=float(raw.get("atr15") or 0),
        er_4h=float(raw.get("er_4h") or 0),
        confirm_close=float(raw.get("confirm_close") or 0),
    )


def dump_engine(engine: Any) -> dict[str, Any]:
    """Сериализация PaperEngine + зеркало PortfolioGuard."""
    from .paper_engine import PaperPending, PaperPos

    book = engine.book
    pending: dict[str, Any] = {}
    for sym, pend in book.pending.items():
        assert isinstance(pend, PaperPending)
        pending[sym] = {
            "signal": _signal_to_dict(pend.signal),
            "limit_px": float(pend.limit_px),
            "qty": float(pend.qty),
            "leg_usd": float(pend.leg_usd),
            "created_bar_ts": int(pend.created_bar_ts),
            "expire_after_bars": int(pend.expire_after_bars),
            "bars_lived": int(pend.bars_lived),
            "crossed_at_place": bool(pend.crossed_at_place),
        }
    positions: dict[str, Any] = {}
    for sym, pos in book.positions.items():
        assert isinstance(pos, PaperPos)
        positions[sym] = {
            "signal": _signal_to_dict(pos.signal),
            "side": pos.side,
            "qty": float(pos.qty),
            "entry": float(pos.entry),
            "sl": float(pos.sl),
            "tp": float(pos.tp),
            "leg_usd": float(pos.leg_usd),
            "fees": float(pos.fees),
            "funding": float(pos.funding),
            "slip": float(pos.slip),
            "open_ts": int(pos.open_ts),
        }
    st = engine.portfolio.state
    return {
        "version": 1,
        "shadow": bool(engine.shadow),
        "equity": float(engine.equity),
        "book": {
            "pending": pending,
            "positions": positions,
            "realized": float(book.realized),
            "last_funding_by_symbol": dict(book.last_funding_by_symbol or {}),
        },
        "portfolio": {
            "open_positions": dict(st.open_positions),
            "pending_entries": dict(st.pending_entries),
            "day_key": st.day_key,
            "day_realized_pnl": float(st.day_realized_pnl),
            "equity": float(st.equity),
            "peak_equity": float(st.peak_equity),
            "kill": bool(st.kill),
            "kill_reason": str(st.kill_reason or ""),
        },
    }


def load_into_engine(engine: Any, payload: dict[str, Any] | None) -> bool:
    """Восстановить book+portfolio. True если что-то загрузили."""
    if not payload or not isinstance(payload, dict):
        return False
    from .paper_engine import PaperBook, PaperPending, PaperPos

    try:
        book_raw = payload.get("book") or {}
        pending: dict[str, PaperPending] = {}
        for sym, raw in (book_raw.get("pending") or {}).items():
            pending[str(sym).upper()] = PaperPending(
                signal=_signal_from_dict(raw["signal"]),
                limit_px=float(raw["limit_px"]),
                qty=float(raw["qty"]),
                leg_usd=float(raw["leg_usd"]),
                created_bar_ts=int(raw["created_bar_ts"]),
                expire_after_bars=int(raw.get("expire_after_bars") or 2),
                bars_lived=int(raw.get("bars_lived") or 0),
                crossed_at_place=bool(raw.get("crossed_at_place") or False),
            )
        positions: dict[str, PaperPos] = {}
        for sym, raw in (book_raw.get("positions") or {}).items():
            positions[str(sym).upper()] = PaperPos(
                signal=_signal_from_dict(raw["signal"]),
                side=str(raw.get("side") or raw["signal"]["side"]),
                qty=float(raw["qty"]),
                entry=float(raw["entry"]),
                sl=float(raw["sl"]),
                tp=float(raw["tp"]),
                leg_usd=float(raw["leg_usd"]),
                fees=float(raw.get("fees") or 0),
                funding=float(raw.get("funding") or 0),
                slip=float(raw.get("slip") or 0),
                open_ts=int(raw.get("open_ts") or 0),
            )
        funding_map = book_raw.get("last_funding_by_symbol")
        if not isinstance(funding_map, dict):
            # миграция со старого глобального слота
            legacy = str(book_raw.get("last_funding_slot") or "")
            funding_map = {s: legacy for s in positions} if legacy else {}
        engine.book = PaperBook(
            pending=pending,
            positions=positions,
            realized=float(book_raw.get("realized") or 0),
            last_funding_by_symbol={str(k).upper(): str(v) for k, v in funding_map.items()},
        )
        engine.equity = float(payload.get("equity") or engine.cfg.start_equity_usd)

        port = payload.get("portfolio") or {}
        st = engine.portfolio.state
        st.open_positions = {
            str(k).upper(): dict(v) for k, v in (port.get("open_positions") or {}).items()
        }
        st.pending_entries = {
            str(k).upper(): dict(v) for k, v in (port.get("pending_entries") or {}).items()
        }
        # синхрон со слотами book, если portfolio пустой/устарел
        if not st.open_positions and positions:
            for sym, pos in positions.items():
                st.open_positions[sym] = {"entry": pos.entry, "qty": pos.qty}
        if not st.pending_entries and pending:
            for sym, pend in pending.items():
                st.pending_entries[sym] = {
                    "limit": pend.limit_px,
                    "bar_ts": pend.created_bar_ts,
                }
        st.day_key = str(port.get("day_key") or st.day_key)
        st.day_realized_pnl = float(port.get("day_realized_pnl") or 0)
        st.equity = float(port.get("equity") or engine.equity)
        st.peak_equity = float(port.get("peak_equity") or st.equity)
        st.kill = bool(port.get("kill") or False)
        st.kill_reason = str(port.get("kill_reason") or "")
        engine.portfolio.update_equity(engine.equity)
        return True
    except Exception:
        logger.exception("[linear] paper_book load failed")
        return False


def save_engine(engine: Any, path: Path) -> None:
    try:
        _atomic_write(path, dump_engine(engine))
    except Exception:
        logger.exception("[linear] paper_book save failed → %s", path)


def try_load(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else None
    except Exception:
        logger.warning("[linear] paper_book unreadable: %s", path)
        return None
