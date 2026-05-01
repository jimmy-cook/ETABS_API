"""Verify MS1 in the same ETABS session as define_mass_source_default (pipeline logic)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.etabs_api import EtabsConnection, _read_mass_source_definition
from etabs_ml.structured_analysis_export import define_mass_source_default


def _prop_mass_ok(sap_model) -> tuple[bool, str]:
    pm = sap_model.PropMaterial
    fn = getattr(pm, "GetMassSource_1", None)
    if not callable(fn):
        return False, "no GetMassSource_1"
    try:
        ret = fn()
    except Exception as exc:
        return False, str(exc)
    if not isinstance(ret, (list, tuple)) or len(ret) < 7:
        return False, f"unexpected ret {ret!r}"
    ie, iam, il, nloads, pats, sfs, code = ret[0], ret[1], ret[2], ret[3], ret[4], ret[5], ret[-1]
    return int(code) == 0, (
        f"retcode={code} elements={ie} added={iam} loads={il} n={nloads} "
        f"pats={pats!r} sfs={list(sfs) if sfs is not None else None!r}"
    )


def main() -> int:
    root = Path(__file__).resolve().parent
    edb = root / "models" / "asce716_full_workflow_test.EDB"
    if not edb.is_file():
        print("MISSING", edb)
        return 1

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
    print("=== define_mass_source_default report ===")
    print(json.dumps(rep, indent=2))

    fields, rows = _read_mass_source_definition(sm)
    ok_prop, prop_msg = _prop_mass_ok(sm)

    print("\n=== Mass Source Definition table (after apply) ===")
    print("fields:", fields)
    for r in rows or []:
        print("row:", r)

    print("\n=== PropMaterial global mass from loads (GetMassSource_1) ===")
    print("ok:", ok_prop)
    print(prop_msg)

    conn.close(save_model=False)

    manifests = sorted(
        (root / "test_mass_verify_out").glob("**/manifest.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    manifest_ok = None
    if manifests:
        m = json.loads(manifests[0].read_text(encoding="utf-8"))
        manifest_ok = m.get("steps", {}).get("mass_source", {}).get("ok")
        print("\n=== Latest pipeline manifest mass_source.ok ===")
        print(manifest_ok)

    ms1_row = next((r for r in (rows or []) if r and str(r[0]).strip() == "MS1"), None)
    idx_loads = fields.index("SourceLoads") if fields and "SourceLoads" in fields else -1
    loads_yes = (
        ms1_row is not None
        and idx_loads >= 0
        and str(ms1_row[idx_loads]).strip().lower() == "yes"
    )
    success = bool(rep.get("ok")) and ms1_row is not None and loads_yes and ok_prop
    print("\n=== Verdict ===")
    print("define_mass_source_default ok:", rep.get("ok"))
    print("MS1 row present:", ms1_row is not None)
    print("MS1 SourceLoads=Yes:", loads_yes)
    print("PropMaterial GetMassSource_1 ok:", ok_prop)
    print("Last saved manifest mass_source.ok (separate run):", manifest_ok)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
