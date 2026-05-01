"""
Sample 5 models in MKS (kN, m, °C) and 5 in US/FPS (kip, ft, °F) using the same
layout as main.py (USER_SETTINGS config).

Usage (from project root):
    python run_sample_mks_us_10.py

Set FULL_WORKFLOW = False to skip ETABS and only validate geometry (fast test).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict

_REPO = Path(__file__).resolve().parent
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.run_primitive_ml_dataset import run_primitive_generation

# True  = build .edb, run analysis, export responses (full pipeline; needs ETABS).
# False = no COM; sampling + summary only.
FULL_WORKFLOW = True

# If True, also dumps every table via API (very slow for many models). Usually False for samples.
SAMPLE_EXPORT_ALL_TABLES = False


def _run_one(
    *,
    units: str,
    output_folder: str,
    limit: int,
    seed: int,
    base: Dict[str, Any],
) -> int:
    s = copy.deepcopy(base)
    s["output_folder"] = output_folder
    s["limit"] = limit
    s["no_etabs"] = not FULL_WORKFLOW
    if FULL_WORKFLOW:
        s["with_analysis_responses"] = bool(base.get("with_analysis_responses", True))
        s["export_all_available_tables"] = SAMPLE_EXPORT_ALL_TABLES
    else:
        s["with_analysis_responses"] = False
        s["export_all_available_tables"] = False
    s["max_draws"] = 0
    s["config"]["etabs"]["units"] = units
    s["config"]["sampling"]["seed"] = seed
    ai = s["analysis_inputs"]
    cfg = copy.deepcopy(s["config"])
    cfg["with_analysis_responses"] = bool(s["with_analysis_responses"])
    cfg["export_all_available_tables"] = bool(s["export_all_available_tables"])
    cfg["analysis_inputs"] = copy.deepcopy(ai)
    return run_primitive_generation(
        cfg,
        Path(s["output_folder"]),
        no_etabs=bool(s["no_etabs"]),
        limit=int(s["limit"]),
        max_draws=int(s["max_draws"]),
        with_analysis_responses=None,
        super_dead_value=float(ai["super_dead_value"]),
        live_value=float(ai["live_value"]),
        seismic_x_name=str(ai["seismic_x_name"]),
        seismic_y_name=str(ai["seismic_y_name"]),
        seismic_ss=float(ai["seismic_ss"]),
        seismic_s1=float(ai["seismic_s1"]),
        seismic_r=float(ai["seismic_r"]),
        seismic_site_class=int(ai["seismic_site_class"]),
        export_all_available_tables=None,
    )


def main() -> int:
    from main import USER_SETTINGS

    base = copy.deepcopy(USER_SETTINGS)
    r1 = _run_one(
        units="mks",
        output_folder="ml_full_v2_5_mks",
        limit=5,
        seed=42,
        base=base,
    )
    r2 = _run_one(
        units="us",
        output_folder="ml_full_v2_5_us",
        limit=5,
        seed=142,
        base=base,
    )
    return 0 if r1 == 0 and r2 == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
