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
    ap.add_argument("--sizing", choices=["fixed", "risk_clamp"], default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.audit:
        from .audit_parity import run_audit

        return run_audit(hours=args.hours, shadow=args.shadow)

    from .config import default_config
    from .desk_linear import LinearDesk

    paper = not args.live
    cfg = default_config(paper=paper, sizing=args.sizing)
    if args.live and cfg.paper:
        cfg.paper = False
    desk = LinearDesk(cfg)
    try:
        desk.start()
    except KeyboardInterrupt:
        desk.stop()
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
