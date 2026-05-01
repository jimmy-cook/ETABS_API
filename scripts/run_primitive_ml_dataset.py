#!/usr/bin/env python3
"""CLI: primitive ML dataset batch (adds ``src`` to path if package not installed)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.run_primitive_ml_dataset import main

if __name__ == "__main__":
    raise SystemExit(main())
