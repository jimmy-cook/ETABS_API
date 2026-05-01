#!/usr/bin/env python3
"""Full workflow smoke: parametric frame + ASCE 7-16 loads → ``models/asce716_full_workflow_test.edb``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.asce7_16_load_setup import (
    Asce716LoadConfig,
    Asce716SeismicASCE716Params,
    Asce716WindASCE716Params,
    setup_asce716_loads,
)
from etabs_ml.etabs_api import EtabsConnection
from etabs_ml.etabs_units import resolve_units
from etabs_ml.parametric_building_dimensions import building_to_flat_dict, resolve_building
from etabs_ml.parametric_definitions import iter_param_dicts
from etabs_ml.run_parametric_dataset import build_etabs_frame_model


def _structural_ok(report: dict) -> bool:
    return bool(
        report.get("patterns", {}).get("ok")
        and report.get("combos", {}).get("ok")
        and report.get("slabs", {}).get("ok")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="ASCE 7-16 full workflow test model (geometry + loads)")
    ap.add_argument(
        "--out",
        type=Path,
        default=_REPO / "models" / "asce716_full_workflow_test.edb",
        help="Output .edb path",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=_REPO / "models" / "asce716_full_workflow_test_report.json",
        help="JSON report path",
    )
    ap.add_argument("--ly-ref", type=float, default=24.0, help="Reference plan dimension Ly (m)")
    ap.add_argument("--stories", type=int, default=3, help="Number of above-ground stories")
    ap.add_argument(
        "--units",
        default="mks",
        help="Unit preset for InitializeNewModel (see etabs_units.UNIT_PRESETS)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Require full setup_asce716_loads ok (including auto lateral wind)",
    )
    args = ap.parse_args()

    present_units, length_scale = resolve_units(str(args.units))
    combo = next(iter_param_dicts())
    rb = resolve_building(combo, Ly_ref=float(args.ly_ref), n_stories=int(args.stories))
    flat_meta = building_to_flat_dict(rb, combo)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    conn = EtabsConnection(attach_to_active=False)
    conn.connect()
    sm = conn.sap_model

    mesh = build_etabs_frame_model(
        sm,
        rb,
        mat_name="C40",
        dead_pattern="DEAD",
        present_units=present_units,
        length_scale_from_metres=length_scale,
        add_default_dead_pattern=False,
    )

    cfg = Asce716LoadConfig(
        dead="DEAD",
        live="LIVE",
        dead_uniform=-3.0,
        live_uniform=-2.0,
        super_dead_uniform=-1.0,
        dead_self_weight_multiplier=1.0,
        seismic_x_params=Asce716SeismicASCE716Params(),
        seismic_y_params=Asce716SeismicASCE716Params(direction=2),
        wind_params=Asce716WindASCE716Params(),
    )
    report = setup_asce716_loads(sm, cfg)

    ret_save = sm.File.Save(str(args.out.resolve()))
    conn.close(save_model=False)

    payload = {
        "edb": str(args.out.resolve()),
        "units_preset": str(args.units),
        "present_units": present_units,
        "length_scale_from_metres": length_scale,
        "building": flat_meta,
        "mesh": mesh,
        "load_setup": report,
        "file_save_ret": ret_save,
        "structural_ok": _structural_ok(report),
        "full_ok": bool(report.get("ok")),
    }
    args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("Saved model:", args.out.resolve())
    print("Saved report:", args.report.resolve())
    print("Mesh:", mesh)
    print("File.Save return:", ret_save)
    print("structural_ok (patterns+combos+slabs):", payload["structural_ok"])
    print("full_ok (includes auto lateral):", payload["full_ok"])

    edb_ok = args.out.is_file()
    if args.strict:
        return 0 if edb_ok and report.get("ok") else 1
    return 0 if edb_ok and payload["structural_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
