#!/usr/bin/env python3
"""Backward-compatible shim -> agent_bus.hub."""

import pathlib
import sys

_BUS_PKG = pathlib.Path(__file__).resolve().parents[3] / "bus"
if _BUS_PKG.is_dir() and str(_BUS_PKG) not in sys.path:
    sys.path.insert(0, str(_BUS_PKG))

from agent_bus.hub import main  # noqa: E402

if __name__ == "__main__":
    main()
