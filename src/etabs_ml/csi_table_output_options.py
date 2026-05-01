"""
CSI OAPI: Database Table Output Options.

- ``Results.Setup.SetOption*`` affects general results setup.
- ``DatabaseTables.SetOutputOptionsForDisplay`` / ``SetTableOutputOptionsForDisplay``
  control what ``GetTableForDisplayArray`` returns (envelope vs step-by-step).

Both are applied so the UI matches extracted tables.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


DEFAULT_TABLE_OUTPUT_OPTIONS: Dict[str, Any] = {
    "multistep_static": 1,
    "nonlinear_static": 1,
    "modal_history": 1,
    # Direct integration / linear-dynamic style DB tables (CSI "Direct history" column).
    # Defaults to envelopes; do not mirror multistep_static so UI can choose independently.
    "direct_history": 1,
    "load_combo_multiple": 1,
    "mode_shape_option": 1,
    "mode_shape_start": 1,
    "mode_shape_end": 12,
    "buckling_option": 1,
    "buckling_start": 1,
    "buckling_end": 6,
    "base_react_option": 1,
    "base_react_x": 0.0,
    "base_react_y": 0.0,
    "base_react_z": 0.0,
    # Optional: only used when ``DatabaseTables.SetTableOutputOptionsForDisplay`` exists.
    "steady_state": 1,
    "steady_state_option": 1,
    "power_spectral_density": 1,
    "bridge_design": 1,
}

_ALLOWED_KEYS = frozenset(DEFAULT_TABLE_OUTPUT_OPTIONS.keys())


def merge_table_output_options(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_TABLE_OUTPUT_OPTIONS)
    if not user or not isinstance(user, dict):
        return out
    for k, v in user.items():
        if k not in _ALLOWED_KEYS:
            continue
        try:
            if k.startswith("base_react_") and k != "base_react_option":
                out[k] = float(v)
            else:
                out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def parse_table_output_options_json(raw: Optional[str]) -> Dict[str, Any]:
    """Parse URL/body JSON string; invalid input returns defaults only."""
    if raw is None:
        return dict(DEFAULT_TABLE_OUTPUT_OPTIONS)
    s = str(raw).strip()
    if not s:
        return dict(DEFAULT_TABLE_OUTPUT_OPTIONS)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return dict(DEFAULT_TABLE_OUTPUT_OPTIONS)
    if not isinstance(data, dict):
        return dict(DEFAULT_TABLE_OUTPUT_OPTIONS)
    return merge_table_output_options(data)


def _com_return_ok(ret: Any) -> bool:
    if ret is None:
        return True
    if isinstance(ret, (list, tuple)) and len(ret) > 0:
        try:
            return int(ret[0]) == 0
        except (TypeError, ValueError):
            return True
    try:
        return int(ret) == 0
    except (TypeError, ValueError):
        return True


def apply_database_tables_output_options_for_display(
    sap_model, opts: Optional[Dict[str, Any]], *, quiet: bool = False
) -> None:
    """
    Set options on ``SapModel.DatabaseTables`` that drive ``GetTableForDisplayArray``.

    Without this, ``Results.Setup`` alone often leaves tables in envelope mode
    (especially linear / multistep static). CSI exposes either:

    - ``SetOutputOptionsForDisplay`` (15 args, older typelib), or
    - ``SetTableOutputOptionsForDisplay`` (18 args, newer ETABS).
    """
    o = merge_table_output_options(opts)
    dt = getattr(sap_model, "DatabaseTables", None)
    if dt is None:
        if not quiet:
            print("[WARN] SapModel.DatabaseTables missing — cannot set table display options")
        return

    is_user_base = o["base_react_option"] == 2
    bx = float(o["base_react_x"]) if is_user_base else 0.0
    by = float(o["base_react_y"]) if is_user_base else 0.0
    bz = float(o["base_react_z"]) if is_user_base else 0.0
    is_all_modes = bool(o["mode_shape_option"] == 1)
    is_all_buck = bool(o["buckling_option"] == 1)
    ms = int(o["multistep_static"])
    nl = int(o["nonlinear_static"])
    mh = int(o["modal_history"])
    combo = int(o["load_combo_multiple"])
    dh = int(o.get("direct_history", 1))

    # --- Newer ETABS: SetTableOutputOptionsForDisplay ---
    st = getattr(dt, "SetTableOutputOptionsForDisplay", None)
    if callable(st):
        try:
            ret = st(
                bx,
                by,
                bz,
                is_all_modes,
                int(o["mode_shape_start"]),
                int(o["mode_shape_end"]),
                is_all_buck,
                int(o["buckling_start"]),
                int(o["buckling_end"]),
                mh,
                dh,
                nl,
                ms,
                int(o.get("steady_state", 1)),
                int(o.get("steady_state_option", 1)),
                int(o.get("power_spectral_density", 1)),
                combo,
                int(o.get("bridge_design", 1)),
            )
            if _com_return_ok(ret):
                if not quiet:
                    print(
                        "[OK] DatabaseTables.SetTableOutputOptionsForDisplay "
                        f"(MultistepStatic={ms}, NLStatic={nl}, ModalHist={mh}, DirectHist={dh}, Combo={combo})"
                    )
                _log_database_table_output_options(dt, newer=True, quiet=quiet)
                return
            else:
                if not quiet:
                    print(f"[WARN] DatabaseTables.SetTableOutputOptionsForDisplay returned {ret!r}")
        except Exception as ex:
            if not quiet:
                print(f"[WARN] DatabaseTables.SetTableOutputOptionsForDisplay skipped: {ex}")

    # --- Older: SetOutputOptionsForDisplay ---
    so = getattr(dt, "SetOutputOptionsForDisplay", None)
    if callable(so):
        try:
            ret = so(
                is_user_base,
                bx,
                by,
                bz,
                is_all_modes,
                int(o["mode_shape_start"]),
                int(o["mode_shape_end"]),
                is_all_buck,
                int(o["buckling_start"]),
                int(o["buckling_end"]),
                ms,
                nl,
                mh,
                dh,
                combo,
            )
            if _com_return_ok(ret):
                if not quiet:
                    print(
                        "[OK] DatabaseTables.SetOutputOptionsForDisplay "
                        f"(MultistepStatic={ms}, NLStatic={nl}, ModalHist={mh}, DirectHist={dh}, Combo={combo})"
                    )
                _log_database_table_output_options(dt, newer=False, quiet=quiet)
            else:
                if not quiet:
                    print(f"[WARN] DatabaseTables.SetOutputOptionsForDisplay returned {ret!r}")
        except Exception as ex:
            if not quiet:
                print(f"[WARN] DatabaseTables.SetOutputOptionsForDisplay skipped: {ex}")
    else:
        if not quiet:
            print("[WARN] No DatabaseTables.SetTableOutputOptionsForDisplay / SetOutputOptionsForDisplay on this build")


def apply_results_setup_table_output_options(
    sap_model, opts: Optional[Dict[str, Any]], *, quiet: bool = False
) -> None:
    """
    Call SapModel.Results.Setup setters for database table output behavior.

    Integer codes follow the ETABS dialog pattern used in CSI samples:
      multistep_static / nonlinear_static / modal_history: 1=Envelopes, 2=Step-by-step, 3=Last Step
      load_combo_multiple: 1=Envelopes, 2=Multiple Values If Possible
      mode_shape_option / buckling_option: 1=All Modes, 2=Some Modes
      base_react_option: 1=Program Determined, 2=User Specified (uses x,y,z)
    """
    o = merge_table_output_options(opts)
    setup = sap_model.Results.Setup

    def _call(method: str, *args: Any) -> None:
        fn = getattr(setup, method, None)
        if not callable(fn):
            if not quiet:
                print(f"[WARN] Results.Setup.{method} not available on this CSI build")
            return
        try:
            ret = fn(*args)
            # Late-bound COM often returns None on success; CSI integer APIs use 0 = OK.
            if ret is None:
                code = 0
            elif isinstance(ret, (list, tuple)) and len(ret) > 0:
                try:
                    code = int(ret[0])
                except (TypeError, ValueError):
                    code = 0
            else:
                try:
                    code = int(ret)
                except (TypeError, ValueError):
                    code = 0
            if code != 0:
                if not quiet:
                    print(f"[WARN] Results.Setup.{method} returned {code}")
            else:
                if not quiet:
                    print(f"[OK] Results.Setup.{method}({', '.join(repr(a) for a in args)})")
        except Exception as ex:
            if not quiet:
                print(f"[WARN] Results.Setup.{method} skipped: {ex}")

    # Base reaction: typelib uses SetOptionBaseReactLoc(GX, GY, GZ) only — three floats.
    # Program-determined → (0,0,0); user-specified → stored coordinates.
    if int(o["base_react_option"]) == 2:
        _call(
            "SetOptionBaseReactLoc",
            float(o["base_react_x"]),
            float(o["base_react_y"]),
            float(o["base_react_z"]),
        )
    else:
        _call("SetOptionBaseReactLoc", 0.0, 0.0, 0.0)
    _call(
        "SetOptionModeShape",
        o["mode_shape_start"],
        o["mode_shape_end"],
        bool(o["mode_shape_option"] == 1),
    )
    _call(
        "SetOptionBucklingMode",
        o["buckling_start"],
        o["buckling_end"],
        bool(o["buckling_option"] == 1),
    )
    _call("SetOptionMultiStepStatic", o["multistep_static"])
    _call("SetOptionNLStatic", o["nonlinear_static"])
    _call("SetOptionMultiValuedCombo", o["load_combo_multiple"])
    # CSI typelib names are SetOptionDirectHist / SetOptionModalHist (not *History).
    _call("SetOptionDirectHist", o["direct_history"])
    _call("SetOptionModalHist", o["modal_history"])

    # Required for GetTableForDisplayArray envelope vs step-by-step (separate from Results.Setup).
    apply_database_tables_output_options_for_display(sap_model, o, quiet=quiet)


def _last_retval(values: Any) -> Any:
    if isinstance(values, (list, tuple)) and values:
        return values[-1]
    return values


def _log_database_table_output_options(dt: Any, newer: bool, *, quiet: bool = False) -> None:
    """Best-effort readback so logs show what ETABS actually accepted."""
    getter_name = "GetTableOutputOptionsForDisplay" if newer else "GetOutputOptionsForDisplay"
    getter = getattr(dt, getter_name, None)
    if not callable(getter):
        return
    if quiet:
        return
    try:
        values = getter()
    except Exception as ex:
        print(f"[WARN] DatabaseTables.{getter_name} readback skipped: {ex}")
        return
    if not _com_return_ok(_last_retval(values)):
        print(f"[WARN] DatabaseTables.{getter_name} readback returned {values!r}")
        return
    if not isinstance(values, (list, tuple)):
        print(f"[INFO] Database table output readback: {values!r}")
        return
    try:
        if newer:
            print(
                "[INFO] Database table output readback: "
                f"ModalHist={values[9]}, DirectHist={values[10]}, "
                f"NLStatic={values[11]}, MultistepStatic={values[12]}, Combo={values[16]}"
            )
        else:
            print(
                "[INFO] Database table output readback: "
                f"MultistepStatic={values[10]}, NLStatic={values[11]}, "
                f"ModalHist={values[12]}, DirectHist={values[13]}, Combo={values[14]}"
            )
    except Exception:
        print(f"[INFO] Database table output readback: {values!r}")
