#!/usr/bin/env python3
"""Smoke: blank model + ASCE 7-16 load setup → ``models/asce716_smoke_test.edb``."""
from __future__ import annotations

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
from etabs_ml.etabs_api import EtabsConnection, EtabsModel

OUT = _REPO / "models" / "asce716_smoke_test.edb"
REPORT = _REPO / "models" / "asce716_smoke_test_report.json"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    conn = EtabsConnection(attach_to_active=False)
    conn.connect()
    sm = conn.sap_model
    mod = EtabsModel(sm)
    mod.initialize_new_model(6)
    mod.create_blank_model()

    cfg = Asce716LoadConfig(
        dead="Dead",
        live="Live",
        dead_uniform=0.0,
        live_uniform=0.0,
        seismic_x_params=Asce716SeismicASCE716Params(),
        seismic_y_params=Asce716SeismicASCE716Params(direction=2),
        wind_params=Asce716WindASCE716Params(),
    )
    report = setup_asce716_loads(sm, cfg)
    ret = sm.File.Save(str(OUT))
    conn.close(save_model=False)

    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Saved model:", OUT)
    print("Saved report:", REPORT)
    print("File.Save return:", ret)
    print("report['ok']:", report.get("ok"))
    return 0 if report.get("ok") and Path(OUT).is_file() else 1


if __name__ == "__main__":
    raise SystemExit(main())
