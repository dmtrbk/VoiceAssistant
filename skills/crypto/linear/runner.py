#!/usr/bin/env python3
"""CLI: paper/live linear desk.

  .venv/bin/python -m skills.crypto.linear.runner --paper
  .venv/bin/python -m skills.crypto.linear.runner --live
  .venv/bin/python -m skills.crypto.linear.runner --audit --hours 48
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bybit linear Paper/Live desk")
    ap.add_argument("--paper", action="store_true", help="paper mode (default)")
    ap.add_argument("--live", action="store_true", help="live trading (requires keys)")
    ap.add_argument("--audit", action="store_true", help="run parity audit and exit")
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--both", action="store_true", help="audit Core+Shadow")
    ap.add_argument("--journal-dir", default=None)
    ap.add_argument("--sizing", choices=["fixed", "risk_clamp"], default=None)
    ap.add_argument(
        "--equity",
        type=float,
        default=None,
        help="paper start equity USD (default from settings, usually 120)",
    )
    args = ap.parse_args(argv)

    if args.audit:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        from .audit_parity import run_audit

        return run_audit(
            hours=args.hours,
            shadow=args.shadow,
            both=args.both or args.shadow,
            journal_dir=args.journal_dir,
        )

    from .config import default_config
    from .desk_linear import LinearDesk
    from .logging_setup import setup_logging

    paper = not args.live
    cfg = default_config(paper=paper, sizing=args.sizing)
    if args.live and cfg.paper:
        cfg.paper = False
    if args.equity is not None:
        cfg.start_equity_usd = float(args.equity)
        # пол kill должен быть ниже старта, иначе paper сразу гаснет
        if cfg.kill_equity_floor_usd >= cfg.start_equity_usd:
            cfg.kill_equity_floor_usd = max(50.0, cfg.start_equity_usd * 0.8)
    log_path = setup_logging(cfg)
    print(f"linear logs: {log_path}")
    print(f"linear journals: {cfg.runtime_dir}")
    desk = LinearDesk(cfg)
    try:
        desk.start()
    except KeyboardInterrupt:
        desk.stop()
        logging.getLogger(__name__).info("[linear] stopped by user")
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
