"""
ETABS post-model workflow: diaphragms on floor slabs (per story), mass source,
linear static (+ optional modal) analysis, and CSV export in a fixed folder tree
matching the Analysis Results browser layout.

Run (ETABS installed), from repo after ``pip install -e .``::

  etabs-structured-export --edb path\\to\\model.edb --out exports\\run1

Or: ``python -m etabs_ml.structured_analysis_export --edb ... --out ...``

Mapping each model to stored data:
  --out / MY_RUN_NAME / <model_stem>_<YYYYMMDD_HHMMSS> /
    manifest.json          # model path, ETABS table keys used, row counts, status
    Analysis Results/
      0_All_Available_Tables/  # one CSV per key from GetAllTables (or GetAvailableTables)
      1_Run_Information/   ...
      2_Joint_Output/        ...
      3_Element_Output/      ...
      4_Structure_Output/    ...

Full database export is **on by default** after analysis. Pass ``--no-export-all-available``
for a faster run (structured tree only).

Optional CSI controls (see ``csi_present_units.py``, ``csi_table_output_options.py``):

- ``--present-units`` — ``SapModel.SetPresentUnits`` using a string id (``kN_m_C``, ``kip_ft_F``, …).
- ``--table-output-json`` — merge overrides into default table output options before CSV export.
- ``--no-csi-table-output-options`` — skip applying those options (legacy export behavior).
- ``--asce716-load-combos-json`` — optional JSON file or inline object (``apply``, ``design_sets``, …) to add ASCE-style response combinations via ``apply_response_combinations`` (see ``asce716_load_config_from_analysis_inputs``).

Requires ``pandas``. Imports :class:`DatabaseTables` from ``etabs_ml.database_tables``.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


import pandas as pd

from .asce7_16_load_setup import (
    Asce716LoadConfig,
    Asce716SeismicASCE716Params,
    apply_custom_response_combinations,
    apply_load_patterns,
    apply_response_combinations,
    apply_ubc97_auto_seismic_patterns,
    asce716_load_config_from_analysis_inputs,
    ensure_wind_load_pattern_exists,
    normalize_seismic_design_code,
    parse_custom_combos_from_dict,
    should_run_asce716_combo_section,
    ubc97_params_from_mapping,
)
from .database_tables import DatabaseTables

from .csi_present_units import (
    ETABS_PRESENT_UNITS,
    allowed_present_units_for_rag_product,
    apply_set_present_units,
    normalize_csi_product,
)
from .csi_table_output_options import (
    apply_results_setup_table_output_options,
    merge_table_output_options,
    parse_table_output_options_json,
)

from .etabs_api import EtabsAnalysis, EtabsConnection, EtabsLoading, EtabsModel


def _sanitize_filename(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", str(name))
    s = re.sub(r"\s+", "_", s).strip("._") or "table"
    return s[:180]


def _csi_ok(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, (list, tuple)) and len(val) > 0:
        try:
            return int(val[0]) == 0
        except (TypeError, ValueError):
            return True
    try:
        return int(val) == 0
    except (TypeError, ValueError):
        return True


def _get_load_case_names(sap_model) -> List[str]:
    try:
        names = sap_model.LoadCases.GetNameList()[1]
    except (TypeError, IndexError, AttributeError):
        return []
    return [str(n) for n in names if n is not None and not str(n).startswith("~")]


def _get_combo_names(sap_model) -> List[str]:
    try:
        names = sap_model.RespCombo.GetNameList()[1]
    except (TypeError, IndexError, AttributeError):
        return []
    return [str(n) for n in names if n is not None and not str(n).startswith("~")]


def _load_case_type(sap_model, name: str) -> Optional[int]:
    try:
        return int(sap_model.LoadCases.GetTypeOAPI(name)[0])
    except Exception:
        return None


def _pick_dead_pattern_name(sap_model) -> str:
    try:
        pats = list(sap_model.LoadPatterns.GetNameList()[1])
    except (TypeError, IndexError, AttributeError):
        pats = []
    for cand in ("Dead", "DEAD", "D", "SDL", "DL"):
        if cand in pats:
            return cand
    sap_model.LoadPatterns.Add("Dead", 1, 1.0, True)
    return "Dead"


def _pick_live_pattern_name(sap_model) -> str:
    """Prefer an existing live-style pattern name on the model."""
    try:
        pats = list(sap_model.LoadPatterns.GetNameList()[1])
    except (TypeError, IndexError, AttributeError):
        pats = []
    for cand in ("Live", "LIVE", "L"):
        if cand in pats:
            return cand
    return "Live"


def _get_floor_area_names(sap_model) -> List[str]:
    try:
        areas = list(sap_model.AreaObj.GetNameList()[1])
    except (TypeError, IndexError, AttributeError):
        return []
    floor_areas: List[str] = []
    for area in areas:
        try:
            orient = sap_model.AreaObj.GetDesignOrientation(area)[0]
        except Exception:
            continue
        if int(orient) == 2:
            floor_areas.append(area)
    return floor_areas


def _ensure_load_pattern(sap_model, name: str, load_type: int) -> Dict[str, Any]:
    try:
        existing = list(sap_model.LoadPatterns.GetNameList()[1])
    except Exception:
        existing = []
    if name in existing:
        return {"name": name, "load_type": load_type, "created": False}
    ret = sap_model.LoadPatterns.Add(name, int(load_type), 0.0, True)
    return {"name": name, "load_type": load_type, "created": _csi_ok(ret)}


def _set_area_uniform_load(sap_model, area_name: str, pattern_name: str, value: float) -> bool:
    # Delegate to etabs_api (multiple SetLoadUniform signatures / typelib versions).
    return bool(EtabsLoading(sap_model).assign_area_uniform_load(
        str(area_name), str(pattern_name), float(value), 6
    ))


def assign_floor_area_loads(
    sap_model,
    *,
    super_dead_pattern: str = "SUPERDEAD",
    super_dead_value: float = 1.0,
    live_pattern: str = "LIVE",
    live_value: float = 2.0,
) -> Dict[str, Any]:
    """
    Assign uniform area loads to all floor slabs. User can override pattern names
    and values from CLI.
    """
    floor_areas = _get_floor_area_names(sap_model)
    sd = _ensure_load_pattern(sap_model, super_dead_pattern, load_type=2)
    ll = _ensure_load_pattern(sap_model, live_pattern, load_type=3)

    sd_ok = 0
    ll_ok = 0
    for area in floor_areas:
        if _set_area_uniform_load(sap_model, area, super_dead_pattern, super_dead_value):
            sd_ok += 1
        if _set_area_uniform_load(sap_model, area, live_pattern, live_value):
            ll_ok += 1
    return {
        "floor_area_count": len(floor_areas),
        "super_dead": {
            "pattern": super_dead_pattern,
            "value": float(super_dead_value),
            "pattern_info": sd,
            "assigned_ok": sd_ok,
        },
        "live": {
            "pattern": live_pattern,
            "value": float(live_value),
            "pattern_info": ll,
            "assigned_ok": ll_ok,
        },
    }


def ensure_linear_static_dead_case(sap_model, case_name: str = "STATIC_DEAD") -> str:
    """Define a linear-static case with the Dead pattern if the model has none."""
    names = _get_load_case_names(sap_model)
    for n in names:
        if _load_case_type(sap_model, n) == 1:
            return n
    dead = _pick_dead_pattern_name(sap_model)
    sap_model.LoadCases.StaticLinear.SetCase(case_name)
    n = 1
    types = ("Load",)
    loads = (dead,)
    sfs = (1.0,)
    sap_model.LoadCases.StaticLinear.SetLoads(case_name, n, types, loads, sfs)
    return case_name


def ensure_modal_eigen_case(sap_model, case_name: str = "MODAL", n_modes: int = 12) -> str:
    """
    Ensure an eigen modal load case exists with ``n_modes`` active.

    If a modal case already exists, its mode count is refreshed (so we do not
    silently skip modal when the model was built elsewhere). Returns the modal
    case name used.
    """
    names = _get_load_case_names(sap_model)
    for n in names:
        if _load_case_type(sap_model, n) == 3:
            nm = str(n)
            try:
                sap_model.LoadCases.ModalEigen.SetNumberModes(nm, int(n_modes), 1)
            except Exception:
                pass
            return nm
    if case_name not in names:
        sap_model.LoadCases.ModalEigen.SetCase(case_name)
    sap_model.LoadCases.ModalEigen.SetNumberModes(case_name, int(n_modes), 1)
    return str(case_name)


def diaphragm_name_for_story(story: str) -> str:
    raw = (story or "BASE").strip() or "BASE"
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw)
    return f"D_{safe}"[:50]


def assign_diaphragms_to_floor_areas_by_story(sap_model) -> Dict[str, Any]:
    """
    One rigid diaphragm per ETABS story that has floor-type areas; assigns each
    floor slab to the diaphragm for its object story (AreaObj.GetLabelFromName).
    """
    floor_areas = _get_floor_area_names(sap_model)

    by_story: Dict[str, List[str]] = {}
    for a in floor_areas:
        try:
            _lab, story, _rest = sap_model.AreaObj.GetLabelFromName(a)
        except Exception:
            story = ""
        key = str(story).strip() if story is not None else ""
        by_story.setdefault(key, []).append(a)

    em = EtabsModel(sap_model)
    created: List[str] = []
    for story, alist in by_story.items():
        dname = diaphragm_name_for_story(story)
        if not em.define_diaphragm(dname, is_rigid=True):
            pass
        created.append(dname)
        for area in alist:
            sap_model.AreaObj.SetDiaphragm(area, dname)

    return {
        "floor_area_count": len(floor_areas),
        "stories": list(by_story.keys()),
        "diaphragms": sorted(set(created)),
        "areas_per_story": {k: len(v) for k, v in by_story.items()},
    }


def _list_load_pattern_names(sap_model) -> List[str]:
    try:
        raw = list(sap_model.LoadPatterns.GetNameList()[1])
    except (TypeError, IndexError, AttributeError):
        return []
    return [str(x) for x in raw if x is not None and not str(x).startswith("~")]


def _resolve_load_pattern_name(name: str, existing: set) -> Optional[str]:
    """Return the model's exact load-pattern spelling if ``name`` matches case-insensitively."""
    if not name or not existing:
        return None
    s = str(name).strip()
    if s in existing:
        return s
    low = s.lower()
    for x in existing:
        if str(x).lower() == low:
            return str(x)
    return None


def _canonicalize_mass_patterns(
    patterns: List[str],
    multipliers: List[float],
    existing: set,
) -> Tuple[List[str], List[float]]:
    """Drop or rename patterns so every name exists on ``sap_model`` (avoids SetMassSource errors)."""
    out_p: List[str] = []
    out_m: List[float] = []
    for p, m in zip(patterns, multipliers):
        c = _resolve_load_pattern_name(p, existing)
        if c and c not in out_p:
            out_p.append(c)
            out_m.append(float(m))
    return out_p, out_m


def define_mass_source_default(
    sap_model,
    name: str = "MS1",
    *,
    dead_pattern: Optional[str] = None,
    super_dead_pattern: Optional[str] = None,
    live_pattern: Optional[str] = None,
    live_mass_factor: float = 0.25,
    sdl_pattern: str = "SDL",
    sdl_mass_factor: float = 1.0,
    include_dead_in_mass_loads: bool = False,
    include_super_dead_in_mass_loads: bool = False,
    add_masses: bool = False,
) -> Dict[str, Any]:
    """
    Default named mass source for modal / dynamic work (default name **MS1**).

    Matches a typical **Mass Source** dialog:

    - **Element self mass** — on (``add_elements=True``).
    - **Additional mass** — off by default (``add_masses=False``).
    - **Specified load patterns** — **Live** at ``live_mass_factor`` (default ``0.25``)
      and **SDL** (or ``sdl_pattern``) at ``sdl_mass_factor`` (default ``1.0``) when
      those patterns exist on the model.

    Set ``include_dead_in_mass_loads`` / ``include_super_dead_in_mass_loads`` to also
    add **Dead** / super-dead patterns to the load-mass table (legacy behaviour).

    Falls back to element-only mass if the full ``SetMassSource`` call fails.
    """
    existing = set(_list_load_pattern_names(sap_model))
    patterns: List[str] = []
    multipliers: List[float] = []
    dead_resolved: Optional[str] = None

    if include_dead_in_mass_loads:
        dead = dead_pattern or _pick_dead_pattern_name(sap_model)
        c = _resolve_load_pattern_name(dead, existing)
        if c:
            dead_resolved = c
            patterns.append(c)
            multipliers.append(1.0)

    if include_super_dead_in_mass_loads:
        sdp = str(super_dead_pattern).strip() if super_dead_pattern else ""
        if sdp:
            c = _resolve_load_pattern_name(sdp, existing)
            if c and c not in patterns:
                patterns.append(c)
                multipliers.append(1.0)

    liv = str(live_pattern).strip() if live_pattern else _pick_live_pattern_name(sap_model)
    c_live = _resolve_load_pattern_name(liv, existing)
    if c_live:
        patterns.append(c_live)
        multipliers.append(float(live_mass_factor))

    c_sdl: Optional[str] = None
    sdl_candidates: List[str] = []
    for cand in (
        str(sdl_pattern).strip(),
        str(super_dead_pattern).strip() if super_dead_pattern else "",
        "SDL",
        "SUPERDEAD",
    ):
        if cand and cand not in sdl_candidates:
            sdl_candidates.append(cand)
    for cand in sdl_candidates:
        r = _resolve_load_pattern_name(cand, existing)
        if r and r not in patterns:
            c_sdl = r
            break
    if c_sdl:
        patterns.append(c_sdl)
        multipliers.append(float(sdl_mass_factor))

    patterns, multipliers = _canonicalize_mass_patterns(patterns, multipliers, existing)

    em = EtabsModel(sap_model)
    attempts: List[Dict[str, Any]] = []
    ok = False
    # Preference: element self mass + specified patterns; then element-only.
    variants = [
        ("loads_and_elements", True, bool(add_masses), True, patterns, multipliers),
        ("elements_only", True, bool(add_masses), False, [], []),
    ]
    for tag, ae, am, al, pats, mults in variants:
        flag = em.define_mass_source(
            name=str(name),
            add_elements=ae,
            add_masses=am,
            add_loads=al,
            is_default=True,
            load_patterns=pats,
            multipliers=mults,
        )
        attempts.append(
            {
                "name": tag,
                "add_elements": ae,
                "add_masses": am,
                "add_loads": al,
                "patterns": list(pats),
                "multipliers": list(mults),
                "ok": bool(flag),
            }
        )
        if flag:
            ok = True
            break

    return {
        "mass_source": name,
        "dead_pattern": dead_resolved,
        "sdl_pattern": str(sdl_pattern).strip(),
        "load_patterns": patterns,
        "multipliers": multipliers,
        "ok": ok,
        "attempts": attempts,
    }


def try_assign_modal_mass_source(sap_model, modal_case: str, mass_source_name: str) -> Dict[str, Any]:
    """
    Some ETABS builds require the eigen modal case to reference a mass source by name.
    Try common OAPI spellings; failures are non-fatal (returned in the dict).
    """
    out: Dict[str, Any] = {
        "modal_case": str(modal_case),
        "mass_source": str(mass_source_name),
        "ok": False,
    }
    me = sap_model.LoadCases.ModalEigen
    for meth_name in ("SetMassSource", "SetMassSourceName", "SetMassSource_1"):
        fn = getattr(me, meth_name, None)
        if not callable(fn):
            continue
        for args in (
            (str(modal_case), str(mass_source_name)),
            (str(modal_case), str(mass_source_name), True),
            (str(mass_source_name), str(modal_case)),
        ):
            try:
                ret = fn(*args)
                if _csi_ok(ret):
                    out["ok"] = True
                    out["method"] = meth_name
                    return out
            except TypeError:
                continue
            except Exception as exc:
                out["last_error"] = str(exc)
    return out


def ensure_asce716_auto_seismic(
    sap_model,
    *,
    seismic_x_name: str = "QX",
    seismic_y_name: str = "QY",
    ss: float = 1.0,
    s1: float = 0.4,
    r: float = 8.0,
    site_class: int = 3,
) -> Dict[str, Any]:
    """
    Create ASCE 7-16 auto seismic lateral patterns (QX/QY) using the dedicated
    load setup module. This addresses workflows where only gravity/slab loads were
    previously assigned.
    """
    cfg = Asce716LoadConfig(
        include_seismic=True,
        include_wind=False,
        include_special_lateral=False,
        seismic_x=seismic_x_name,
        seismic_y=seismic_y_name,
        seismic_x_params=Asce716SeismicASCE716Params(
            direction=1,
            ss=float(ss),
            s1=float(s1),
            r=float(r),
            site_class=int(site_class),
        ),
        seismic_y_params=Asce716SeismicASCE716Params(
            direction=2,
            ss=float(ss),
            s1=float(s1),
            r=float(r),
            site_class=int(site_class),
        ),
        dead_uniform=0.0,
        live_uniform=0.0,
        super_dead_uniform=0.0,
    )
    report = apply_load_patterns(sap_model, cfg)
    return {
        "ok": bool(report.get("ok", False)),
        "auto_lateral_ok": bool(report.get("auto_lateral_ok", False)),
        "seismic_design_code": "asce7_16",
        "auto_lateral_standard": "ASCE 7-16",
        "seismic_x": seismic_x_name,
        "seismic_y": seismic_y_name,
        "ss": float(ss),
        "s1": float(s1),
        "r": float(r),
        "site_class": int(site_class),
        "detail": report,
    }


def ensure_ubc97_auto_seismic(
    sap_model,
    *,
    seismic_x_name: str = "QX",
    seismic_y_name: str = "QY",
    ubc97: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create **1997 UBC** auto seismic lateral patterns using ``apply_ubc97_auto_seismic_patterns``.

    This path does **not** use ASCE 7-16 (no Ss/S1, no ``SetASCE716``). The shared module name
    ``asce7_16_load_setup`` is only a file layout; lateral loads here follow **UBC 1997** inputs.

    Pass optional ``ubc97`` dict (from JSON ``analysis_inputs.ubc97``) with keys matching
    :class:`asce7_16_load_setup.Ubc97SeismicParams` for ``LoadPatterns.AutoSeismic.SetUBC97``
    (e.g. ``period_flag``, ``ct``, ``z``, ``soil_profile``, ``r``, ``top_story``, ``bottom_story``).
    """
    params = ubc97_params_from_mapping(ubc97)
    report = apply_ubc97_auto_seismic_patterns(
        sap_model,
        seismic_x=str(seismic_x_name),
        seismic_y=str(seismic_y_name),
        params=params,
    )
    return {
        "ok": bool(report.get("ok", False)),
        "auto_lateral_ok": bool(report.get("auto_lateral_ok", False)),
        "seismic_design_code": "ubc97",
        "auto_lateral_standard": "1997 UBC",
        "seismic_x": seismic_x_name,
        "seismic_y": seismic_y_name,
        "ubc97": {f.name: getattr(params, f.name) for f in fields(params)},
        "detail": report,
    }


def load_table_output_options_from_cli_arg(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Load merged CSI table output options from a JSON file path or an inline JSON object string.
    Returns ``None`` if ``raw`` is empty (caller may still apply built-in defaults separately).
    """
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    p = Path(s)
    if p.is_file():
        return parse_table_output_options_json(p.read_text(encoding="utf-8"))
    return parse_table_output_options_json(s)


def set_database_tables_cases_and_combos(sap_model) -> None:
    """Select all user load cases and combinations for DatabaseTables export (no extra RunAnalysis)."""
    cases = _get_load_case_names(sap_model)
    combos = _get_combo_names(sap_model)
    sap_model.DatabaseTables.SetLoadCasesSelectedForDisplay(cases)
    sap_model.DatabaseTables.SetLoadCombinationsSelectedForDisplay(combos)


def ensure_results_setup_all_cases_combos(sap_model) -> Dict[str, Any]:
    """
    Select every load case and response combination for **Results** output.

    ETABS often omits modal / story / drift database tables from
    ``GetAvailableTables`` until the corresponding cases are selected for output
    in ``Results.Setup`` (separate from ``DatabaseTables.SetLoadCasesSelectedForDisplay``).
    """
    cases = _get_load_case_names(sap_model)
    combos = _get_combo_names(sap_model)
    try:
        sap_model.Results.Setup.DeselectAllCasesAndCombosForOutput()
    except Exception:
        pass
    n_case_ok = 0
    n_combo_ok = 0
    case_err: List[str] = []
    combo_err: List[str] = []
    for lc in cases:
        ret: Any = None
        try:
            ret = sap_model.Results.Setup.SetCaseSelectedForOutput(str(lc), True)
        except TypeError:
            try:
                ret = sap_model.Results.Setup.SetCaseSelectedForOutput(str(lc))
            except Exception as exc:
                case_err.append(f"{lc}: {exc}")
                continue
        except Exception as exc:
            case_err.append(f"{lc}: {exc}")
            continue
        if _csi_ok(ret):
            n_case_ok += 1
    for cb in combos:
        ret = None
        try:
            ret = sap_model.Results.Setup.SetComboSelectedForOutput(str(cb), True)
        except TypeError:
            try:
                ret = sap_model.Results.Setup.SetComboSelectedForOutput(str(cb))
            except Exception as exc:
                combo_err.append(f"{cb}: {exc}")
                continue
        except Exception as exc:
            combo_err.append(f"{cb}: {exc}")
            continue
        if _csi_ok(ret):
            n_combo_ok += 1
    return {
        "load_case_count": len(cases),
        "combo_count": len(combos),
        "cases_selected_ok": n_case_ok,
        "combos_selected_ok": n_combo_ok,
        "case_errors_sample": case_err[:8],
        "combo_errors_sample": combo_err[:8],
    }


def resolve_table_key(db: DatabaseTables, candidates: Sequence[str]) -> Optional[str]:
    """Map GUI-style names to exact keys in GetAvailableTables()."""
    try:
        available = list(db.SapModel.DatabaseTables.GetAvailableTables()[1])
    except Exception:
        return None
    if not available:
        return None

    def _norm(text: str) -> str:
        t = str(text).lower().strip()
        t = t.replace("&", " and ")
        t = re.sub(r"\btable\s*:\s*", "", t)
        t = re.sub(r"[^a-z0-9]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    avail_set = set(available)
    norm_to_original: Dict[str, str] = {}
    for table in available:
        norm_to_original.setdefault(_norm(table), table)

    # 1) exact / prefixed
    for c in candidates:
        if c in avail_set:
            return c
        prefixed = f"Table: {c}"
        if prefixed in avail_set:
            return prefixed

    # 2) case-insensitive exact
    lower_index = {t.lower(): t for t in available}
    for c in candidates:
        cl = c.lower()
        if cl in lower_index:
            return lower_index[cl]
        p = f"table: {cl}"
        if p in lower_index:
            return lower_index[p]

    # 3) normalized exact
    for c in candidates:
        n = _norm(c)
        if n in norm_to_original:
            return norm_to_original[n]

    # 4) unambiguous normalized substring
    for c in candidates:
        cn = _norm(c)
        hits = [t for t in available if cn and cn in _norm(t)]
        if len(hits) == 1:
            return hits[0]

    # 4b) substring with disambiguation (multiple GUI/API name variants)
    for c in candidates:
        cn = _norm(c)
        if not cn or len(cn) < 6:
            continue
        hits = [t for t in available if cn in _norm(t)]
        if len(hits) > 1:
            best_hit: Optional[str] = None
            best_sc = 0.0
            for t in hits:
                sc = difflib.SequenceMatcher(None, cn, _norm(t)).ratio()
                if sc > best_sc:
                    best_sc = sc
                    best_hit = t
            if best_hit is not None and best_sc >= 0.55:
                return best_hit
        elif len(hits) == 1:
            return hits[0]

    # 5) fuzzy best-match fallback (helps with 'And'/'and', punctuation, spacing)
    best_key: Optional[str] = None
    best_score = 0.0
    for c in candidates:
        cn = _norm(c)
        for t in available:
            tn = _norm(t)
            if not cn or not tn:
                continue
            score = difflib.SequenceMatcher(None, cn, tn).ratio()
            # small boost for token containment
            c_tokens = set(cn.split())
            t_tokens = set(tn.split())
            if c_tokens and c_tokens.issubset(t_tokens):
                score += 0.15
            if score > best_score:
                best_score = score
                best_key = t
    if best_key is not None and best_score >= 0.72:
        return best_key
    return None


# Folder tree exactly mirroring the Analysis Results browser shown by user images.
# Each inner list item is ONE expected table with aliases as fallback names.
STRUCTURED_EXPORT_TREE: List[Tuple[str, List[Tuple[Optional[str], List[List[str]]]]]] = [
    (
        "Analysis Results/1_Run_Information",
        [
            (None, [["Program Control"]]),
            (None, [["Analysis Messages"]]),
            (None, [["Active Degrees of Freedom", "Analysis Options - Active Degrees of Freedom"]]),
            (None, [["Solver Options", "Analysis Options - Solver Options", "Analysis Options - SAPFire Options"]]),
        ],
    ),
    (
        "Analysis Results/2_Joint_Output/Displacements",
        [
            (None, [["Joint Displacements"]]),
            (None, [["Joint Displacements (Including Internal Mesh Joints)"]]),
            (None, [["Joint Drifts"]]),
            (None, [["Diaphragm Center Of Mass Displacements"]]),
            (None, [["Diaphragm Max Over Avg Drifts"]]),
            (None, [["Story Drifts"]]),
            (None, [["Story Max Over Avg Displacements"]]),
            (None, [["Story Max Over Avg Drifts"]]),
        ],
    ),
    (
        "Analysis Results/2_Joint_Output/Reactions",
        [
            (None, [["Joint Reactions"]]),
            (None, [["Integrated Wall Reactions"]]),
            (None, [["Joint Design Reactions"]]),
        ],
    ),
    (
        "Analysis Results/2_Joint_Output/Joint_Masses",
        [
            (None, [["Assembled Joint Masses"]]),
        ],
    ),
    (
        "Analysis Results/3_Element_Output/Frame_Output",
        [
            (None, [["Element Forces - Columns"]]),
            (None, [["Element Forces - Beams"]]),
            (None, [["Element Joint Forces - Frame"]]),
        ],
    ),
    (
        "Analysis Results/3_Element_Output/Area_Output",
        [
            (None, [["Element Forces - Area Shells"]]),
            (None, [["Element Stresses - Area Shells"]]),
            (None, [["Element Strains - Area Shells"]]),
            (None, [["Element Joint Forces - Shells"]]),
        ],
    ),
    (
        "Analysis Results/3_Element_Output/Wall_Output",
        [
            (None, [["Pier Forces"]]),
        ],
    ),
    (
        "Analysis Results/3_Element_Output/Objects and Elements",
        [
            (None, [["Objects and Elements - Joints"]]),
            (None, [["Objects and Elements - Frames"]]),
            (None, [["Objects and Elements - Areas"]]),
        ],
    ),
    (
        "Analysis Results/4_Structure_Output/Base_Reactions",
        [
            (None, [["Base Reactions"]]),
        ],
    ),
    (
        "Analysis Results/4_Structure_Output/Modal_Information",
        [
            (
                None,
                [
                    [
                        "Modal Periods And Frequencies",
                        "Modal Periods and Frequencies",
                        "Modal Periods & Frequencies",
                        "Modal Case Results - Periodic",
                    ]
                ],
            ),
            (
                None,
                [
                    [
                        "Modal Participating Mass Ratios",
                        "Modal Participating Mass Ratios by Story",
                        "Modal Participating Mass Ratios - Sorted by Story",
                    ]
                ],
            ),
            (
                None,
                [
                    [
                        "Modal Load Participation Ratios",
                        "Modal Load Participation Ratios by Story",
                        "Modal Load Participation Ratios - Sorted by Story",
                    ]
                ],
            ),
            (
                None,
                [
                    [
                        "Modal Participation Factors",
                        "Modal Participation Factors by Story",
                        "Modal Participation Factors - Sorted by Story",
                    ]
                ],
            ),
            (
                None,
                [
                    [
                        "Modal Direction Factors",
                        "Modal Direction Factors by Story",
                        "Modal Direction Factors - Sorted by Story",
                    ]
                ],
            ),
        ],
    ),
    (
        "Analysis Results/4_Structure_Output/Other_Output_Items",
        [
            (None, [["Centers Of Mass And Rigidity"]]),
            (None, [["Story Forces"]]),
            (None, [["Diaphragm Forces"]]),
            (None, [["Story Stiffness"]]),
            (None, [["Tributary Area and LLRF"]]),
        ],
    ),
]


def export_tables_to_disk(
    sap_model,
    base_dir: Path,
    *,
    db: Optional[DatabaseTables] = None,
) -> List[Dict[str, Any]]:
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    db = db or DatabaseTables(SapModel=sap_model)
    rows_out: List[Dict[str, Any]] = []

    for rel, groups in STRUCTURED_EXPORT_TREE:
        folder = base_dir.joinpath(*rel.split("/"))
        folder.mkdir(parents=True, exist_ok=True)
        for _sub, cand_groups in groups:
            for candidates in cand_groups:
                key = resolve_table_key(db, candidates)
                label = candidates[0]
                safe = _sanitize_filename(label)
                dest = folder / f"{safe}.csv"
                entry: Dict[str, Any] = {
                    "folder": rel.replace("\\", "/"),
                    "requested": list(candidates),
                    "resolved_key": key,
                    "file": str(dest.relative_to(base_dir)).replace("\\", "/"),
                    "rows": 0,
                    "status": "missing",
                }
                if not key:
                    rows_out.append(entry)
                    continue
                try:
                    df = db.read(key, to_dataframe=True)
                except Exception as exc:  # pragma: no cover
                    entry["status"] = f"error:{exc}"
                    rows_out.append(entry)
                    continue
                if df is None or getattr(df, "empty", True):
                    entry["status"] = "empty"
                    rows_out.append(entry)
                    continue
                df.to_csv(dest, index=False)
                entry["rows"] = int(len(df))
                entry["status"] = "ok"
                rows_out.append(entry)
    return rows_out


def export_all_available_tables_to_disk(
    sap_model,
    base_dir: Path,
    *,
    db: Optional[DatabaseTables] = None,
) -> List[Dict[str, Any]]:
    """
    Export every table available from cDatabaseTables.
    Preference:
      1) GetAllTables (with IsEmpty filtering when discoverable)
      2) fallback to GetAvailableTables
    """
    base_dir = Path(base_dir)
    folder = base_dir / "Analysis Results" / "0_All_Available_Tables"
    folder.mkdir(parents=True, exist_ok=True)
    db = db or DatabaseTables(SapModel=sap_model)
    dt = db.SapModel.DatabaseTables
    available: List[str] = []

    # Prefer GetAllTables to capture broader key set across ETABS versions.
    try:
        all_tables = dt.GetAllTables()
        if isinstance(all_tables, (list, tuple)) and len(all_tables) >= 2:
            # Usually includes names + import type + is-empty flags (order varies by version)
            list_items = [x for x in all_tables if isinstance(x, (list, tuple))]
            names = next((list(x) for x in list_items if x and isinstance(x[0], str)), [])
            bool_arrays = [list(x) for x in list_items if x and isinstance(x[0], bool)]
            empty_flags: List[bool] = []
            if bool_arrays:
                # prefer the bool array with same length as names
                same = [b for b in bool_arrays if len(b) == len(names)]
                if same:
                    empty_flags = same[0]
            if names:
                if empty_flags and len(empty_flags) == len(names):
                    available = [n for n, is_empty in zip(names, empty_flags) if not is_empty]
                    # ETABS often marks every row "empty" until results are refreshed; filtering
                    # down to [] would skip exporting entirely. Fall back to all names.
                    if not available:
                        available = list(names)
                else:
                    available = names
    except Exception:
        available = []

    # Fallback: display-available tables
    if not available:
        try:
            available = list(dt.GetAvailableTables()[1])
        except Exception:
            return []

    # Deduplicate while preserving order
    seen: set = set()
    unique_keys: List[str] = []
    for k in available:
        if k in seen:
            continue
        seen.add(k)
        unique_keys.append(k)

    rows_out: List[Dict[str, Any]] = []
    for key in unique_keys:
        safe = _sanitize_filename(key)
        dest = folder / f"{safe}.csv"
        entry: Dict[str, Any] = {
            "folder": "Analysis Results/0_All_Available_Tables",
            "requested": [key],
            "resolved_key": key,
            "file": str(dest.relative_to(base_dir)).replace("\\", "/"),
            "rows": 0,
            "status": "empty",
        }
        try:
            df = db.read(key, to_dataframe=True)
        except Exception as exc:
            entry["status"] = f"error:{exc}"
            rows_out.append(entry)
            continue
        if df is None or getattr(df, "empty", True):
            rows_out.append(entry)
            continue
        df.to_csv(dest, index=False)
        entry["rows"] = int(len(df))
        entry["status"] = "ok"
        rows_out.append(entry)
    return rows_out


def run_pipeline(
    sap_model,
    output_root: Path,
    *,
    model_path: Optional[Path] = None,
    assign_diaphragms: bool = True,
    assign_slab_loads: bool = True,
    super_dead_pattern: str = "SDL",
    super_dead_value: float = 1.0,
    live_pattern: str = "LIVE",
    live_value: float = 2.0,
    seismic_x_name: str = "QX",
    seismic_y_name: str = "QY",
    seismic_design_code: str = "asce7_16",
    seismic_ss: float = 1.0,
    seismic_s1: float = 0.4,
    seismic_r: float = 8.0,
    seismic_site_class: int = 3,
    ubc97: Optional[Dict[str, Any]] = None,
    mass_source: bool = True,
    ensure_auto_seismic: bool = True,
    ensure_modal: bool = True,
    run_analysis: bool = True,
    export_all_available: bool = True,
    save_edb: Optional[bool] = None,
    present_units: Optional[str] = None,
    csi_product: str = "etabs",
    apply_csi_table_output_options: bool = True,
    csi_table_output_options: Optional[Dict[str, Any]] = None,
    asce716_load_combinations: Optional[Dict[str, Any]] = None,
    dead_load_pattern: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full pipeline. Writes under ``output_root / f"{stem}_{timestamp}" /`` and returns manifest dict.

    When ``model_path`` points to an ``.edb`` file, the pipeline **saves that file** by default
    (``File.Save``) so mass source and other edits persist for the next GUI open. Pass
    ``save_edb=False`` to skip overwriting the file (in-memory only, previous behavior).

    By default ``export_all_available`` is True: after analysis, every non-empty table key from
    ``GetAllTables`` (or ``GetAvailableTables``) is written under
    ``Analysis Results/0_All_Available_Tables/`` in addition to the structured tree.

    ``present_units`` — optional CSI string id (``kN_m_C``, ``kip_ft_F``, …) passed to
    ``SapModel.SetPresentUnits`` at the start of the run (omit to keep the model as opened).

    ``apply_csi_table_output_options`` — when True (default), applies ``csi_table_output_options``
    (merged with CSI defaults) via ``Results.Setup`` + ``DatabaseTables`` before CSV export so
    envelope vs step-by-step matches the ETABS GUI.

    ``asce716_load_combinations`` — optional dict (e.g. from JSON ``analysis_inputs``). Use
    ``apply`` / ``template_combos`` for preset ``CONC_*`` / ``STL_*`` / ``SLAB_*`` packs,
    ``custom_combos`` for explicit ``{ "NAME": [["PAT", sf], ...] }`` linear-add combinations,
    ``replace_existing_combos`` to ``RespCombo.Delete`` before adding custom names, and
    ``include_wind`` / ``wind`` when wind terms are needed. Runs after slab loads and auto seismic.
    ``dead_load_pattern`` — dead pattern name for combo templates; if omitted, inferred from the model.
    """
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    stem = Path(model_path).stem if model_path else "active_model"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"{stem}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "model_path": str(model_path) if model_path else None,
        "run_directory": str(run_dir.resolve()),
        "utc_iso": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    if present_units is not None and str(present_units).strip():
        report["steps"]["present_units"] = apply_set_present_units(
            sap_model,
            str(present_units).strip(),
            product=str(csi_product or "etabs"),
            quiet=True,
        )

    if assign_diaphragms:
        report["steps"]["diaphragms"] = assign_diaphragms_to_floor_areas_by_story(sap_model)
    if assign_slab_loads:
        report["steps"]["slab_area_loads"] = assign_floor_area_loads(
            sap_model,
            super_dead_pattern=super_dead_pattern,
            super_dead_value=super_dead_value,
            live_pattern=live_pattern,
            live_value=live_value,
        )
    if ensure_auto_seismic:
        code = normalize_seismic_design_code(seismic_design_code)
        if code == "ubc97":
            report["steps"]["auto_seismic"] = ensure_ubc97_auto_seismic(
                sap_model,
                seismic_x_name=seismic_x_name,
                seismic_y_name=seismic_y_name,
                ubc97=ubc97,
            )
        else:
            report["steps"]["auto_seismic"] = ensure_asce716_auto_seismic(
                sap_model,
                seismic_x_name=seismic_x_name,
                seismic_y_name=seismic_y_name,
                ss=seismic_ss,
                s1=seismic_s1,
                r=seismic_r,
                site_class=seismic_site_class,
            )

    combo_cfg = None
    raw_lc = asce716_load_combinations
    if raw_lc is not None and should_run_asce716_combo_section(raw_lc):
        if dead_load_pattern is not None and str(dead_load_pattern).strip():
            dead_nm = str(dead_load_pattern).strip()
        else:
            dead_nm = _pick_dead_pattern_name(sap_model)
        combo_cfg = asce716_load_config_from_analysis_inputs(
            raw_lc,
            dead=dead_nm,
            live=str(live_pattern),
            super_dead=str(super_dead_pattern),
            seismic_x=str(seismic_x_name),
            seismic_y=str(seismic_y_name),
        )
        wind_prime = False
        wind_nm = "WIND"
        if isinstance(raw_lc, dict) and bool(raw_lc.get("include_wind")):
            wind_prime = True
            wind_nm = str(raw_lc.get("wind", "WIND"))
        if combo_cfg is not None and bool(combo_cfg.include_wind):
            wind_prime = True
            wind_nm = str(combo_cfg.wind)
        if wind_prime:
            ok_w, wmsg = ensure_wind_load_pattern_exists(sap_model, wind_nm)
            report["steps"]["asce716_wind_prime"] = {"ok": ok_w, "detail": wmsg}
        if combo_cfg is not None:
            report["steps"]["asce716_load_combinations"] = apply_response_combinations(
                sap_model, combo_cfg
            )
        custom_defs = parse_custom_combos_from_dict(raw_lc)
        if custom_defs:
            report["steps"]["asce716_custom_load_combinations"] = apply_custom_response_combinations(
                sap_model,
                custom_defs,
                replace_existing=bool(raw_lc.get("replace_existing_combos", False)),
            )
        if bool(raw_lc.get("use_etabs_default_combos", False)):
            try:
                # AddDesignDefaultCombos(DesignSteel, DesignConcrete, DesignAluminum, DesignColdFormed)
                # We map "steel_frame" and "concrete_frame" from design_sets if present, or default to both
                ds = raw_lc.get("design_sets", ["concrete_frame", "steel_frame"])
                ds_str = [str(x).lower() for x in ds]
                ds_steel = "steel_frame" in ds_str
                ds_conc = "concrete_frame" in ds_str
                if not ds_steel and not ds_conc:
                    ds_steel = True
                    ds_conc = True
                ret = sap_model.RespCombo.AddDesignDefaultCombos(ds_steel, ds_conc, False, False)
                report["steps"]["etabs_default_combos"] = {
                    "ok": ret == 0,
                    "detail": f"AddDesignDefaultCombos ret={ret} (Steel={ds_steel}, Concrete={ds_conc})"
                }
            except Exception as e:
                report["steps"]["etabs_default_combos"] = {"ok": False, "detail": f"Exception: {e}"}

    if mass_source:
        report["steps"]["mass_source"] = define_mass_source_default(
            sap_model,
            name="MS1",
            super_dead_pattern=super_dead_pattern,
            live_pattern=live_pattern,
        )

    lc_static = ensure_linear_static_dead_case(sap_model)
    report["steps"]["linear_static_case"] = lc_static
    if ensure_modal:
        modal_nm = ensure_modal_eigen_case(sap_model)
        report["steps"]["modal_case"] = modal_nm
        if mass_source and isinstance(report["steps"].get("mass_source"), dict):
            if report["steps"]["mass_source"].get("ok"):
                ms_nm = str(report["steps"]["mass_source"].get("mass_source") or "MS1")
                report["steps"]["modal_mass_source"] = try_assign_modal_mass_source(
                    sap_model, modal_nm, ms_nm
                )

    if run_analysis:
        try:
            sap_model.SetModelIsLocked(False)
        except Exception:
            pass
        ana = EtabsAnalysis(sap_model)
        analysis_ok = bool(ana.run_analysis())
        report["steps"]["analysis"] = {
            "ok": analysis_ok,
            "message": "RunAnalysis finished" if analysis_ok else "RunAnalysis returned non-zero",
        }
        if not analysis_ok and export_all_available:
            report["steps"]["tables_export_warning"] = (
                "Analysis did not complete successfully; analysis result database tables may be empty."
            )
    elif export_all_available:
        report["steps"]["tables_export_note"] = (
            "run_analysis=False (--no-run): only tables backed by existing analysis data will have rows. "
            "Re-run without --no-run to execute analysis before export."
        )

    report["steps"]["results_setup_all_cases"] = ensure_results_setup_all_cases_combos(sap_model)
    set_database_tables_cases_and_combos(sap_model)
    if apply_csi_table_output_options:
        merged_opts = merge_table_output_options(csi_table_output_options)
        report["steps"]["csi_table_output_options"] = merged_opts
        apply_results_setup_table_output_options(sap_model, merged_opts, quiet=True)
    db = DatabaseTables(SapModel=sap_model)
    report["steps"]["tables"] = export_tables_to_disk(sap_model, run_dir, db=db)
    if export_all_available:
        report["steps"]["all_available_tables"] = export_all_available_tables_to_disk(
            sap_model,
            run_dir,
            db=db,
        )

    # Re-apply the mass source right before saving so analysis cannot have reset the
    # load-pattern multipliers.  This is a no-op when the COM path succeeded in the
    # earlier call; it only matters when the database-table fallback is needed.
    if mass_source:
        report["steps"]["mass_source_presave"] = define_mass_source_default(
            sap_model,
            name="MS1",
            super_dead_pattern=super_dead_pattern,
            live_pattern=live_pattern,
        )

    manifest_path = run_dir / "manifest.json"
    report["manifest_path"] = str(manifest_path.resolve())

    do_save = bool(model_path) if save_edb is None else bool(save_edb)
    if do_save and model_path:
        mp = Path(model_path).resolve()
        if mp.suffix.lower() == ".edb":
            try:
                sap_model.SetModelIsLocked(False)
            except Exception:
                pass
            try:
                sret = sap_model.File.Save(str(mp))
                report["steps"]["edb_save"] = {
                    "path": str(mp),
                    "ret": sret,
                    "ok": _csi_ok(sret),
                }
            except Exception as exc:
                report["steps"]["edb_save"] = {"path": str(mp), "ok": False, "error": str(exc)}

    manifest_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    p = argparse.ArgumentParser(description="ETABS: diaphragms, mass source, analysis, structured CSV export.")
    p.add_argument("--edb", type=str, default="", help="Open this .edb (starts ETABS if not attaching).")
    p.add_argument("--out", type=str, required=True, help="Output root folder; a timestamped subfolder is created per run.")
    p.add_argument("--attach", action="store_true", help="Attach to active ETABS instead of starting a new instance.")
    p.add_argument("--no-diaphragms", action="store_true")
    p.add_argument("--no-slab-loads", action="store_true", help="Skip assigning Super Dead/Live loads to floor slabs.")
    p.add_argument("--super-dead-pattern", type=str, default="SDL", help="Load pattern name for super dead area load.")
    p.add_argument("--super-dead-value", type=float, default=1.0, help="Uniform area load value for super dead (current ETABS force/area units).")
    p.add_argument("--live-pattern", type=str, default="LIVE", help="Load pattern name for live area load.")
    p.add_argument("--live-value", type=float, default=2.0, help="Uniform area load value for live load (current ETABS force/area units).")
    p.add_argument("--no-mass-source", action="store_true")
    p.add_argument("--no-auto-seismic", action="store_true", help="Skip creating auto seismic QX/QY patterns.")
    p.add_argument(
        "--seismic-design-code",
        type=str,
        default="asce7_16",
        choices=["asce7_16", "ubc97"],
        help="Lateral seismic code: ASCE 7-16 (Ss/S1/R/site class) or 1997 UBC (AutoSeismic.SetUBC97 via analysis_inputs.ubc97).",
    )
    p.add_argument("--seismic-x-name", type=str, default="QX", help="Auto seismic load pattern name for X direction.")
    p.add_argument("--seismic-y-name", type=str, default="QY", help="Auto seismic load pattern name for Y direction.")
    p.add_argument("--seismic-ss", type=float, default=1.0, help="ASCE 7-16 short-period spectral acceleration Ss.")
    p.add_argument("--seismic-s1", type=float, default=0.4, help="ASCE 7-16 1-second spectral acceleration S1.")
    p.add_argument("--seismic-r", type=float, default=8.0, help="ASCE 7-16 response modification factor R.")
    p.add_argument("--seismic-site-class", type=int, default=3, help="ASCE site class as integer 1..6 (A..F).")
    p.add_argument("--no-modal", action="store_true", help="Do not add a modal case if missing.")
    p.add_argument(
        "--no-run",
        action="store_true",
        help="Skip RunAnalysis (use only data already in the .edb). Analysis result tables export empty unless the model was analyzed before.",
    )
    p.add_argument(
        "--no-export-all-available",
        action="store_true",
        help="Skip exporting every DatabaseTables key (GetAllTables / GetAvailableTables). "
        "Default is to dump all non-empty tables under Analysis Results/0_All_Available_Tables/.",
    )
    p.add_argument(
        "--export-all-available",
        action="store_true",
        help="No-op (kept for scripts): full table export is already the default.",
    )
    p.add_argument(
        "--no-save-edb",
        action="store_true",
        help="Do not write changes back to the .edb opened with --edb (default is to save).",
    )
    p.add_argument(
        "--present-units",
        type=str,
        default=None,
        metavar="ID",
        help="CSI present-units string for SapModel.SetPresentUnits at the start of the run "
        f"(ETABS examples: {', '.join(sorted(ETABS_PRESENT_UNITS))}). Omit to leave model units unchanged.",
    )
    p.add_argument(
        "--csi-product",
        type=str,
        default="etabs",
        choices=["etabs", "sap2000"],
        help="Allowed present-units ids for validation (SAP2000 allows extra metric variants).",
    )
    p.add_argument(
        "--table-output-json",
        type=str,
        default=None,
        metavar="PATH_OR_JSON",
        help="Path to a JSON file or an inline JSON object merging into CSI default table output "
        "options (multistep_static, modal_history, load_combo_multiple, …). "
        "Ignored if --no-csi-table-output-options is set.",
    )
    p.add_argument(
        "--no-csi-table-output-options",
        action="store_true",
        help="Do not call Results.Setup / DatabaseTables table output setters before export.",
    )
    p.add_argument(
        "--asce716-load-combos-json",
        type=str,
        default=None,
        metavar="PATH_OR_JSON",
        help="JSON file path or inline object: keys apply, design_sets, include_wind, include_seismic, "
        "optional dead/live/super_dead/wind/seismic_x/seismic_y overrides (see asce716_load_config_from_analysis_inputs).",
    )
    args = p.parse_args()

    conn = EtabsConnection(attach_to_active=bool(args.attach))
    conn.connect()
    sm = conn.sap_model
    model_path: Optional[Path] = None

    if args.edb:
        model_path = Path(args.edb).resolve()
        if not model_path.is_file():
            print("File not found:", model_path)
            return 1
        try:
            ret = sm.File.OpenFile(str(model_path))
        except Exception as exc:
            print("OpenFile failed:", exc)
            return 1
        if not _csi_ok(ret):
            print("OpenFile returned non-zero:", ret)

    pu = (args.present_units or "").strip() or None
    if pu:
        allowed = allowed_present_units_for_rag_product(normalize_csi_product(str(args.csi_product)))
        if pu not in allowed:
            print(
                "Invalid --present-units",
                repr(pu),
                "for --csi-product",
                repr(args.csi_product),
                "| allowed:",
                ", ".join(sorted(allowed)),
            )
            return 1

    csi_table_opts = None
    if not args.no_csi_table_output_options and args.table_output_json:
        csi_table_opts = load_table_output_options_from_cli_arg(args.table_output_json)

    asce716_lc: Optional[Dict[str, Any]] = None
    if args.asce716_load_combos_json:
        raw_lc = str(args.asce716_load_combos_json).strip()
        try:
            p_lc = Path(raw_lc)
            if p_lc.is_file():
                asce716_lc = json.loads(p_lc.read_text(encoding="utf-8"))
            else:
                asce716_lc = json.loads(raw_lc)
        except (json.JSONDecodeError, OSError) as ex:
            print("Invalid --asce716-load-combos-json:", ex)
            return 1
        if not isinstance(asce716_lc, dict):
            print("--asce716-load-combos-json must be a JSON object (dict).")
            return 1

    rep = run_pipeline(
        sm,
        Path(args.out),
        model_path=model_path,
        assign_diaphragms=not args.no_diaphragms,
        assign_slab_loads=not args.no_slab_loads,
        super_dead_pattern=args.super_dead_pattern,
        super_dead_value=args.super_dead_value,
        live_pattern=args.live_pattern,
        live_value=args.live_value,
        seismic_x_name=args.seismic_x_name,
        seismic_y_name=args.seismic_y_name,
        seismic_design_code=str(args.seismic_design_code),
        seismic_ss=args.seismic_ss,
        seismic_s1=args.seismic_s1,
        seismic_r=args.seismic_r,
        seismic_site_class=args.seismic_site_class,
        ubc97=None,
        mass_source=not args.no_mass_source,
        ensure_auto_seismic=not args.no_auto_seismic,
        ensure_modal=not args.no_modal,
        run_analysis=not args.no_run,
        export_all_available=bool(not args.no_export_all_available),
        save_edb=not args.no_save_edb,
        present_units=pu,
        csi_product=str(args.csi_product or "etabs"),
        apply_csi_table_output_options=not args.no_csi_table_output_options,
        csi_table_output_options=csi_table_opts,
        asce716_load_combinations=asce716_lc,
        dead_load_pattern=None,
    )
    print(json.dumps({"manifest_path": rep.get("manifest_path"), "run_directory": rep.get("run_directory")}, indent=2))
    conn.close(save_model=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
