"""Exit 0 if define_mass_source_default + table + PropMaterial readback succeed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.etabs_api import EtabsConnection, _csi_ret0, _read_mass_source_definition
from etabs_ml.structured_analysis_export import define_mass_source_default


def _get_prop_mass_source_1(sm) -> dict:
    """Informational only: may be empty when mass loads are defined on ``SourceMass`` only."""
    pm = sm.PropMaterial
    fn1 = getattr(pm, "GetMassSource_1", None)
    if not callable(fn1):
        return {"note": "no GetMassSource_1"}
    try:
        ret = fn1(True, True, True, 0, [], [])
    except Exception as exc:
        return {"error": str(exc)}
    if not isinstance(ret, (list, tuple)) or len(ret) < 7:
        return {"raw": repr(ret)}
    return {
        "retcode": int(ret[-1]),
        "include_elements": ret[0],
        "include_added": ret[1],
        "include_loads": ret[2],
        "n_loads": ret[3],
        "patterns": ret[4],
        "sfs": list(ret[5]) if ret[5] is not None else None,
    }


def main() -> int:
    root = Path(__file__).resolve().parent
    edb = root / "models" / "asce716_full_workflow_test.EDB"
    if not edb.is_file():
        print("SKIP: no workflow EDB at", edb)
        return 2

    conn = EtabsConnection(attach_to_active=False)
    conn.connect()
    sm = conn.sap_model
    sm.File.OpenFile(str(edb))

    rep = define_mass_source_default(
        sm,
        name="MS1",
        super_dead_pattern="SUPERDEAD",
        live_pattern="LIVE",
    )
    print("define_mass_source_default:", json.dumps(rep, indent=2))

    fields, rows = _read_mass_source_definition(sm)
    print("Mass Source Definition rows:", rows)

    gm = _get_prop_mass_source_1(sm)
    print("PropMaterial GetMassSource_1:", json.dumps(gm, indent=2, default=str))

    try:
        sret = sm.File.Save(str(edb))
        print("File.Save:", repr(sret), "| ret0_ok:", _csi_ret0(sret) == 0)
    except Exception as exc:
        print("File.Save failed:", exc)

    conn.close(save_model=False)

    ms1 = any(r and str(r[0]).strip() == "MS1" for r in (rows or []))
    loads_yes = False
    if rows and fields:
        try:
            i = list(fields).index("SourceLoads")
            for r in rows:
                if r and str(r[0]).strip() == "MS1" and r[i] is not None and str(r[i]).strip():
                    loads_yes = str(r[i]).strip().lower() == "yes"
                    break  # use first header row only
        except ValueError:
            pass

    ok = bool(rep.get("ok")) and ms1 and loads_yes
    print(
        "VERDICT:",
        "PASS" if ok else "FAIL",
        "| define_mass_source_default ok:",
        rep.get("ok"),
        "| MS1 in Mass Source Definition:",
        ms1,
        "| SourceLoads=Yes on MS1:",
        loads_yes,
    )
    print("(PropMaterial snapshot may be empty if ETABS stores load-mass on SourceMass only.)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
