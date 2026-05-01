#!/usr/bin/env python3
"""Smoke: blank model + UBC97 auto-seismic → ``models/ubc97_smoke_test.edb``."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.asce7_16_load_setup import _ubc97_get_auto_seismic_code
from etabs_ml.etabs_api import EtabsConnection, EtabsModel
from etabs_ml.run_parametric_dataset import _sync_etabs_stories_from_floor_elevations
from etabs_ml.structured_analysis_export import ensure_ubc97_auto_seismic

OUT = _REPO / "models" / "ubc97_smoke_test.edb"
REPORT = _REPO / "models" / "ubc97_smoke_test_report.json"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    conn = EtabsConnection(attach_to_active=False)
    conn.connect()
    sm = conn.sap_model
    mod = EtabsModel(sm)
    mod.initialize_new_model(6)
    mod.create_blank_model()

    floor_z = [0.0, 3.6576, 7.3152, 10.9728]
    story_res = _sync_etabs_stories_from_floor_elevations(sm, floor_z)

    ubc = {
        "period_flag": 0,
        "ct": 0.0731,
        "z": 0.4,
        "soil_profile": 4,
        "r": 8.5,
        "top_story": "Story3",
        "bottom_story": "Story1",
    }
    rep = ensure_ubc97_auto_seismic(sm, ubc97=ubc)
    readback = {
        "QX": _ubc97_get_auto_seismic_code(sm, "QX"),
        "QY": _ubc97_get_auto_seismic_code(sm, "QY"),
    }

    ret_save = sm.File.Save(str(OUT))
    conn.close(save_model=False)

    payload = {
        "story_setup": story_res,
        "ensure_ubc97": rep,
        "get_auto_seismic_code_after": readback,
        "file_save_ret": ret_save,
    }
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Saved model:", OUT)
    print("Saved report:", REPORT)
    print("auto_lateral_ok:", rep.get("auto_lateral_ok"))
    print("GetAutoSeismicCode readback:", readback)
    ok_file = OUT.is_file()
    ok_lateral = bool(rep.get("auto_lateral_ok"))
    ok_story = bool(story_res.get("ok"))
    return 0 if ok_file and ok_lateral and ok_story else 1


if __name__ == "__main__":
    sys.exit(main())
