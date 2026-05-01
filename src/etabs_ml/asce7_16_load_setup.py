# -*- coding: utf-8 -*-
"""
ASCE 7-16–oriented load patterns, response combinations, and slab uniform loads.

This module is **standalone** after a model exists (``SapModel`` with slabs/frames).

**Seismic / wind (linear static auto-loads)**  
ETABS uses **code-specific** API methods. Some typelibs expose flat
``SetAutoSeismicASCE716`` / ``SetAutoWindASCE716`` on ``LoadPatterns``; others only
expose ``LoadPatterns.AutoSeismic.SetASCE716`` and ``LoadPatterns.AutoWind.SetASCE716``.
Pass optional ``Asce716SeismicASCE716Params`` / ``Asce716WindASCE716Params`` on
``Asce716LoadConfig`` when your build supports the matching setter. The module falls
back to ``SetAutoSeismicCode`` / ``SetAutoWindCode`` when those exist.

Always confirm argument order and enumerations in the **OAPI CHM** for your ETABS build.

**Gravity (user-controlled)**  
Uniform **surface** pressures on **all floor areas** (or a name list you pass) for
dead, live, and super dead - values are in **present force/area units** (e.g. kN/m^2
for kN-m-C, psf for kip-ft-F).

**Lateral**  
- **Wind**: load pattern type **6** (Wind in the generated CSI typelib) or **8** (Other) is tried until ``Add`` succeeds;
  then ``SetAutoWindASCE716`` (if present) or ``LoadPatterns.AutoWind.SetASCE716``, else
  ``SetAutoWindCode``.  
- **Seismic**: type **5** (Quake); then ``SetAutoSeismicASCE716`` (if the typelib has it) or
  ``LoadPatterns.AutoSeismic.SetASCE716`` (VB-style ``nDir`` + parameters), else
  ``SetAutoSeismicCode``.  
- **Special lateral**: ``OTHER`` load pattern for user-defined lateral - no auto generator.

**Combinations**  
Template strength combinations (LRFD-style factors) for **concrete frame**, **steel
frame**, and **slab gravity** sets - *simplified*; verify factors against ASCE 7-16
Sec. 2.3 / project criteria. For JSON-driven **custom** linear-add combos, use
``parse_custom_combos_from_dict`` / ``apply_custom_response_combinations`` (see
``analysis_inputs.asce716_load_combinations.custom_combos`` in the batch config).

Example::

    from etabs_api import EtabsConnection
    from asce7_16_load_setup import (
        Asce716LoadConfig,
        Asce716WindASCE716Params,
        setup_asce716_loads,
    )

    conn = EtabsConnection(attach_to_active=True)
    cfg = Asce716LoadConfig(
        dead_uniform=-1.0,
        live_uniform=-0.5,
        wind_params=Asce716WindASCE716Params(wind_speed=115.0),
    )
    setup_asce716_loads(conn.sap_model, cfg)
    conn.close(False)
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

# CSI load pattern type numbers (ETABS OAPI) — ``eLoadPatternType`` in generated typelib
LTYPE_DEAD = 1
LTYPE_SUPER_DEAD = 2
LTYPE_LIVE = 3
LTYPE_SEISMIC = 5  # eLoadPatternType_Quake
LTYPE_WIND = 6  # eLoadPatternType_Wind (do **not** use 5 for wind — 5 is seismic)
LTYPE_OTHER = 8  # fallback used by ``etabs_api.EtabsLoading.add_auto_wind_pattern``

# Aliases (explicit names; same as CSI enum)
ELOADPATTERN_QUAKE = LTYPE_SEISMIC
ELOADPATTERN_WIND = LTYPE_WIND

# ASCE 7-16 auto-wind in ETABS U.S. practice uses **mph**; convert m/s for API when requested.
_MPS_TO_MPH = 2.2369362920544023


@dataclass
class Asce716SeismicASCE716Params:
    """
    Maps to OAPI ASCE 7-16 auto seismic, typically
    ``SapModel.LoadPatterns.AutoSeismic.SetASCE716`` (see CSI VB/C# examples).

    **Direction** — set ``direction`` to pick the **checkbox family** ETABS uses for
    linear static ASCE 7-16 seismic (matches *Direction and Eccentricity* in the GUI):

    - ``1`` → **+X earthquake**: X Dir, X Dir + Eccentricity, X Dir − Eccentricity.
    - ``2`` → **+Y earthquake**: Y Dir, Y Dir + Eccentricity, Y Dir − Eccentricity.
    - ``3``…``6`` → single checkbox only (advanced; rare).

    Or pass ``n_dir`` explicitly (six booleans, or seven with a legacy leading placeholder)
    to override the mask.

    **Site class** — ``site_class`` uses 1=A … 6=F; it is converted to API
    0..5. Confirm against the CHM for your ETABS build.

    **Period / Ct** — ``ct_type`` (0..3) is the OAPI *CtType*; if ``None`` it is
    inferred from ``ct``. ``user_t`` / ``period_t`` follow the *PeriodFlag* = 3
    user-T convention when ``user_t`` is True. ``x_period`` is used as the
    approximate period value when not using user-defined T (PeriodFlag 0);
    adjust if your OAPI version differs.

    ``user_w`` and ``min_v`` apply only to typelibs that expose the flat
    ``SetAutoSeismicASCE716`` overload; the ``AutoSeismic.SetASCE716`` path does
    not take them.
    """

    direction: int = 1
    n_dir: Optional[Sequence[bool]] = None
    eccentricity: float = 0.05
    ct: float = 0.02
    ct_type: Optional[int] = None
    x_period: float = 0.75
    user_z: bool = False
    top_z: float = 0.0
    bottom_z: float = 0.0
    user_t: bool = False
    period_t: float = 0.0
    user_w: float = 0.0
    ss: float = 1.0
    s1: float = 0.4
    long_period: float = 8.0
    site_class: int = 3
    fa: float = 0.0
    fv: float = 0.0
    r: float = 8.0
    omega: float = 3.0
    cd: float = 5.5
    importance: float = 1.0
    min_v: float = 0.0


@dataclass
class Asce716WindASCE716Params:
    """
    OAPI ASCE 7-16 **auto wind** inputs. Prefer ``LoadPatterns.AutoWind.SetASCE716`` when
    the flat method ``SetAutoWindASCE716`` is not on the typelib.

    Generated CSI typelib signature for this ETABS install::

        SetASCE716(Name, ExposureFrom, DirAngle, Cpw, Cpl, ASCECase,
                   ASCEe1, ASCEe2, UserZ, TopZ, BottomZ, WindSpeed,
                   ExposureType, Kzt, GustFactor, Kd, SolidGrossRatio,
                   UserExposure)

    ``exposure_from``: 1 = extents of rigid diaphragms, 2 = area objects, 3 = frame objects.
    ``exposure_type``: 1=B, 2=C, 3=D (typical; confirm in CHM).
    ``wind_speed``: numeric value; interpret using ``wind_speed_unit`` (``mph`` = ASCE/ETABS
    U.S. auto-wind; ``m_s`` = converted to mph before calling ``SetASCE716``).
    """

    exposure_from: int = 1
    # ``direction`` is kept as an alias for older callers; ``dir_angle`` is what the OAPI expects.
    direction: int = 1
    dir_angle: Optional[float] = None
    angle: float = 0.0
    cpw: float = 0.8
    cpl: float = -0.5
    asce_case: int = 1
    asce_e1: float = 0.0
    asce_e2: float = 0.0
    user_z: bool = False
    top_z: float = 0.0
    bottom_z: float = 0.0
    wind_speed: float = 115.0
    # ``mph`` = pass speed to OAPI as-is (ASCE/ETABS U.S. default). ``m_s`` = convert to mph.
    wind_speed_unit: str = "mph"
    exposure_type: int = 2
    importance: float = 1.0
    kzt: float = 1.0
    kd: float = 0.85
    ke: float = 1.0
    gust_factor: float = 0.85
    solid_gross_ratio: float = 0.2
    user_exposure: bool = False


def normalize_seismic_design_code(value: Optional[str]) -> str:
    """
    Normalize user/JSON input to ``\"asce7_16\"`` or ``\"ubc97\"``.

    Unknown values default to ASCE 7-16 so existing configs keep working.
    """
    if value is None:
        return "asce7_16"
    s = str(value).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if s in ("ubc97", "ubc1997", "1997ubc", "uniformbuildingcode1997"):
        return "ubc97"
    if s in ("asce716", "asce7_16", "asce716seismic", ""):
        return "asce7_16"
    # tolerate "asce7" without edition
    if s.startswith("asce7") or s.startswith("asce"):
        return "asce7_16"
    return "asce7_16"


@dataclass
class Ubc97SeismicParams:
    """
    1997 UBC auto lateral for a Quake pattern. The exporter first calls
    ``LoadPatterns.SetAutoSeismicUBC97`` (fills the **Auto Lateral Load** column on many ETABS builds),
    then falls back to ``LoadPatterns.AutoSeismic.SetUBC97`` if needed.

    **SetUBC97** (fallback): older builds use 14 arguments ending with ``NearSourceA``, ``NearSourceV``,
    ``TopStory``, ``BotStory``. **ETABS 23+** inserts ``UBC97NearSourceFlag`` (int) **after** ``R`` and
    before ``NearSourceA`` / ``NearSourceV`` — use :attr:`near_source_flag` (default ``0``).

    Legacy 14-arg shape::

        SetUBC97(..., I, R, NearSourceA, NearSourceV, TopStory, BotStory)

    Newer 15-arg shape::

        SetUBC97(..., I, R, UBC97NearSourceFlag, NearSourceA, NearSourceV, TopStory, BotStory)

    **PeriodFlag** — ``0`` = program-calculated period (``T`` ignored); other values per ETABS help.

    **Z** — seismic zone factor (e.g. 0.40 for Zone 4).

    **SoilProfile** — 1=SA … 6=SF.

    **TopStory / BotStory** — story **names** (e.g. ``Story5``, ``Base``). Leave blank to auto-pick
    last / first story from ``Story.GetStories()`` (``Base`` is remapped when absent).
    """

    direction: int = 1
    eccentricity: float = 0.05
    period_flag: int = 0
    ct: float = 0.0731
    period_t: float = 0.0
    z: float = 0.40
    soil_profile: int = 4
    importance_i: float = 1.0
    r: float = 8.5
    #: Passed to modern ``AutoSeismic.SetUBC97`` as ``UBC97NearSourceFlag`` (CSI typelib).
    near_source_flag: int = 0
    #: ``UBC97SeismicCoeffFlag`` — ``0`` = program Ca/Cv from Z and soil (typical); non‑zero = user Ca/Cv.
    seismic_coeff_flag: int = 0
    #: User Ca / Cv when ``seismic_coeff_flag`` requests user coefficients (else often ignored).
    ca: float = 0.0
    cv: float = 0.0
    #: Near-source source type and distance (CSI ``UBC97SourceType``, ``UBC97Dist``); use ``0`` if unused.
    source_type: int = 0
    source_distance: float = 0.0
    near_source_na: float = 1.0
    near_source_nv: float = 1.0
    top_story: str = ""
    bottom_story: str = ""


def _ubc97_story_names_from_model(sap_model) -> Tuple[List[str], str]:
    """Return (all story names, diagnostic) from ``Story.GetStories()``."""
    try:
        gs = sap_model.Story.GetStories()
        if not isinstance(gs, (list, tuple)) or len(gs) < 2:
            return [], "GetStories:unexpected_layout"
        names = gs[1]
        if names is None:
            return [], "GetStories:no_names"
        out = [str(x) for x in names]
        return out, "ok"
    except Exception as exc:  # noqa: BLE001
        return [], f"GetStories:{exc}"


def _resolve_ubc97_top_bottom_stories(sap_model, p: Ubc97SeismicParams) -> Tuple[str, str, str]:
    """
    Resolve **TopStory** / **BotStory** names for ``AutoSeismic.SetUBC97``.

    Returns ``(top_story, bottom_story, note)``.
    """
    tt = str(p.top_story or "").strip()
    bt = str(p.bottom_story or "").strip()
    names, _diag = _ubc97_story_names_from_model(sap_model)
    if not names:
        return tt or "Story1", bt or "Story1", "no_stories_from_model"
    if not tt:
        tt = names[-1]
    if not bt:
        bt = names[0]
    # If user asked for "Base" but model has no such label, fall back to first story
    if bt.lower() == "base" and not any(str(n).lower() == "base" for n in names):
        bt = names[0]
        return tt, bt, "bottom_story_Base_replaced_with_first_story"
    return tt, bt, "ok"


def ubc97_params_from_mapping(m: Optional[Dict[str, Any]]) -> Ubc97SeismicParams:
    """Merge a JSON ``ubc97`` object into :class:`Ubc97SeismicParams` (unknown keys ignored)."""
    p = Ubc97SeismicParams()
    if not m:
        return p
    allowed = {f.name for f in fields(Ubc97SeismicParams)}
    aliases = {
        "i": "importance_i",
        "I": "importance_i",
        "Z": "z",
        "DirFlag": "direction",
        "Eccen": "eccentricity",
        "PeriodFlag": "period_flag",
        "T": "period_t",
        "TopStory": "top_story",
        "BotStory": "bottom_story",
        "TopZ": "top_story",
        "BotZ": "bottom_story",
        "top_z": "top_story",
        "bottom_z": "bottom_story",
        "NearSourceA": "near_source_na",
        "NearSourceV": "near_source_nv",
        "UBC97NearSourceFlag": "near_source_flag",
        "near_source_flag": "near_source_flag",
        "UBC97SeismicCoeffFlag": "seismic_coeff_flag",
        "seismic_coeff_flag": "seismic_coeff_flag",
        "UBC97Ca": "ca",
        "UBC97Cv": "cv",
        "UBC97SourceType": "source_type",
        "UBC97Dist": "source_distance",
        "SoilProfile": "soil_profile",
    }
    raw = dict(m)
    # Legacy: integer UBC zone 1–4 → approximate Z if ``z`` not given
    if "z" not in raw and "Z" not in raw and "seismic_zone" in raw:
        zn = int(raw["seismic_zone"])
        z_map = {1: 0.075, 2: 0.15, 3: 0.30, 4: 0.40}
        raw["z"] = float(z_map.get(zn, 0.40))
    updates: Dict[str, Any] = {}
    for raw_k, v in raw.items():
        k = aliases.get(str(raw_k), str(raw_k))
        if k not in allowed:
            continue
        sample = getattr(p, k)
        if isinstance(sample, bool):
            if isinstance(v, bool):
                updates[k] = v
            else:
                updates[k] = str(v).strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(sample, int) and not isinstance(sample, bool):
            updates[k] = int(v)
        elif isinstance(sample, float):
            updates[k] = float(v)
        else:
            updates[k] = str(v)
    out = replace(p, **updates)
    # Legacy: ``user_t`` false meant program period → PeriodFlag 0 (superseded by explicit period_flag)
    if "period_flag" not in raw and "user_t" in raw:
        ut = raw["user_t"]
        prog = (not bool(ut)) if not isinstance(ut, str) else ut.strip().lower() in ("0", "false", "no", "")
        out = replace(out, period_flag=0 if prog else 1)
    return out


def _autoseismic_com_ret_ok(val: Any) -> bool:
    """True if COM returned success for AutoSeismic setters (tuple or int HRESULT)."""
    if val is None:
        return True
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        try:
            return int(val[-1]) == 0
        except (TypeError, ValueError):
            pass
    return _csi_ret0(val) == 0


def _ubc97_user_t_from_period_flag(period_flag: int) -> bool:
    """Flat ``SetAutoSeismicUBC97`` uses ``UserT``: False = program period; True = use ``T``."""
    return int(period_flag) != 0


def _ubc97_get_auto_seismic_code(sap_model, pattern_name: str) -> str:
    """Best-effort readback of assigned auto-seismic code name (for diagnostics)."""
    fn = getattr(sap_model.LoadPatterns, "GetAutoSeismicCode", None)
    if not callable(fn):
        return ""
    try:
        out = fn(str(pattern_name))
        if isinstance(out, (list, tuple)):
            for x in out:
                if isinstance(x, str) and x.strip():
                    return str(x).strip()
            if len(out) >= 1:
                return str(out[0])
        if out is not None:
            return str(out).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _try_set_autoseismic_ubc97_flat(
    sap_model, pattern_name: str, p: Ubc97SeismicParams, top_s: str, bot_s: str
) -> Tuple[bool, str]:
    """
    Many ETABS builds link the **Auto Lateral Load** column via the flat OAPI::

        LoadPatterns.SetAutoSeismicUBC97(Name, DirFlag, Eccen, Ct, Z, SoilProfile, NearSourceA, NearSourceV,
                                         I, R, UserT, T, TopZ, BotZ)

    ``TopZ`` / ``BotZ`` are story **names**. ``UserT`` replaces the separate ``PeriodFlag`` path.
    """
    fn = getattr(sap_model.LoadPatterns, "SetAutoSeismicUBC97", None)
    if not callable(fn):
        return False, "SetAutoSeismicUBC97 not on LoadPatterns"
    user_t = _ubc97_user_t_from_period_flag(p.period_flag)
    try:
        ret = fn(
            str(pattern_name),
            int(p.direction),
            float(p.eccentricity),
            float(p.ct),
            float(p.z),
            int(p.soil_profile),
            float(p.near_source_na),
            float(p.near_source_nv),
            float(p.importance_i),
            float(p.r),
            bool(user_t),
            float(p.period_t),
            str(top_s),
            str(bot_s),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"SetAutoSeismicUBC97 raised: {exc}"
    if _csi_ret0(ret) == 0 or _autoseismic_com_ret_ok(ret):
        rb = _ubc97_get_auto_seismic_code(sap_model, pattern_name)
        extra = f" GetAutoSeismicCode={rb!r}" if rb else ""
        return True, f"SetAutoSeismicUBC97 ok{extra}"
    return False, f"SetAutoSeismicUBC97 ret={ret!r}"


def _ubc97_story_top_bottom_elevations(
    sap_model, top_story: str, bottom_story: str
) -> Tuple[float, float, str]:
    """
    Modern ``SetUBC97`` expects **TopZ** / **BottomZ** as elevations (float), not story labels.

    Reads ``Story.GetStories()`` name and elevation tuples (ETABS 23 returns ``Base`` plus
    ``Story*`` with matching elevation arrays).
    """
    try:
        gs = sap_model.Story.GetStories()
        if not isinstance(gs, (list, tuple)) or len(gs) < 3:
            return float("nan"), float("nan"), "GetStories:unexpected_layout"
        names_raw = gs[1]
        elevs_raw = gs[2]
        if not isinstance(names_raw, (list, tuple)) or not isinstance(elevs_raw, (list, tuple)):
            return float("nan"), float("nan"), "GetStories:missing_names_or_elevs"
        name_list = [str(x) for x in names_raw]
        elist = [float(x) for x in elevs_raw]
        if len(name_list) != len(elist):
            return float("nan"), float("nan"), f"GetStories:len_mismatch:{len(name_list)}!={len(elist)}"

        def elev(label: str) -> float:
            key = str(label or "").strip().lower()
            for i, n in enumerate(name_list):
                if str(n).strip().lower() == key:
                    return float(elist[i])
            return float("nan")

        tz = elev(top_story)
        bz = elev(bottom_story)
        if tz != tz or bz != bz:
            return float("nan"), float("nan"), f"story_not_found:{top_story!r},{bottom_story!r}"
        return tz, bz, "elev_from_GetStories"
    except Exception as exc:  # noqa: BLE001
        return float("nan"), float("nan"), f"GetStories:{exc}"


def _ubc97_soil_profile_to_api(soil_profile: int) -> int:
    """Map JSON ``soil_profile`` 1=SA … 6=SF to CSI ``UBC97SoilProfileType`` 0..5 when in range."""
    s = int(soil_profile)
    if 1 <= s <= 6:
        return s - 1
    if 0 <= s <= 5:
        return s
    return max(0, min(5, s))


def _invoke_autoseismic_set_ubc97_modern(
    fn, canon: str, p: Ubc97SeismicParams, top_s: str, bot_s: str, sap_model
) -> Tuple[Optional[Any], str]:
    """
    ETABS 23+ / current CSI typelib::

        SetUBC97(Name, DirFlag, Eccen, PeriodFlag, Ct, UserT, UserZ, TopZ, BottomZ,
                 UBC97SeismicCoeffFlag, UBC97SoilProfileType, UBC97Z, UBC97Ca, UBC97Cv,
                 UBC97NearSourceFlag, UBC97SourceType, UBC97Dist, UBC97Na, UBC97Nv, UBC97I, UBC97R)
    """
    user_t = _ubc97_user_t_from_period_flag(p.period_flag)
    top_z, bot_z, z_note = _ubc97_story_top_bottom_elevations(sap_model, top_s, bot_s)
    if top_z != top_z or bot_z != bot_z:
        return None, f"TopZ/BottomZ elevation lookup failed ({z_note})"
    try:
        ret = fn(
            str(canon),
            int(p.direction),
            float(p.eccentricity),
            int(p.period_flag),
            float(p.ct),
            bool(user_t),
            False,
            float(top_z),
            float(bot_z),
            int(p.seismic_coeff_flag),
            _ubc97_soil_profile_to_api(p.soil_profile),
            float(p.z),
            float(p.ca),
            float(p.cv),
            int(p.near_source_flag),
            int(p.source_type),
            float(p.source_distance),
            float(p.near_source_na),
            float(p.near_source_nv),
            float(p.importance_i),
            float(p.r),
        )
        return ret, f"SetUBC97_modern ({z_note})"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _invoke_autoseismic_set_ubc97_legacy(
    fn, canon: str, p: Ubc97SeismicParams, top_s: str, bot_s: str
) -> Tuple[Optional[Any], str]:
    """Pre‑2020 style ``SetUBC97`` (14 args) or 15-arg with ``UBC97NearSourceFlag`` only (some builds)."""
    base = (
        str(canon),
        int(p.direction),
        float(p.eccentricity),
        int(p.period_flag),
        float(p.ct),
        float(p.period_t),
        float(p.z),
        int(p.soil_profile),
        float(p.importance_i),
        float(p.r),
    )
    na = float(p.near_source_na)
    nv = float(p.near_source_nv)
    fl = int(p.near_source_flag)
    tb = (str(top_s), str(bot_s))
    trials = (
        ("15_R_flag_NaNv_TB", base + (fl, na, nv) + tb),
        ("15_R_NaNv_flag_TB", base + (na, nv, fl) + tb),
        ("14_R_NaNv_TB", base + (na, nv) + tb),
    )
    errors: List[str] = []
    for label, args in trials:
        try:
            ret = fn(*args)
            return ret, label
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
    return None, " | ".join(errors)


def _invoke_autoseismic_set_ubc97_any(
    fn, canon: str, p: Ubc97SeismicParams, top_s: str, bot_s: str, sap_model
) -> Tuple[Optional[Any], str]:
    ret_m, msg_m = _invoke_autoseismic_set_ubc97_modern(fn, canon, p, top_s, bot_s, sap_model)
    if ret_m is not None:
        return ret_m, msg_m
    ret_l, msg_l = _invoke_autoseismic_set_ubc97_legacy(fn, canon, p, top_s, bot_s)
    if ret_l is not None:
        return ret_l, f"{msg_m}; then {msg_l}"
    return None, f"modern: {msg_m}; legacy: {msg_l}"


def _try_set_autoseismic_ubc97(sap_model, pattern_name: str, p: Ubc97SeismicParams) -> Tuple[bool, str]:
    """
    Assign **1997 UBC** auto lateral to a Quake load pattern.

    1. Prefer ``LoadPatterns.SetAutoSeismicUBC97`` (updates the **Auto Lateral Load** column on many builds).
    2. Else ``LoadPatterns.AutoSeismic.SetUBC97`` (alternate signature with ``PeriodFlag``).
    """
    canon = _resolve_pattern_label(sap_model, str(pattern_name))
    top_s, bot_s, note = _resolve_ubc97_top_bottom_stories(sap_model, p)

    pre_code_parts: List[str] = []
    for code in ("UBC 97", "1997 UBC", "UBC97"):
        ok_pre, msg_pre = _try_set_auto_seismic_code(sap_model, canon, code)
        if ok_pre:
            pre_code_parts.append(f"SetAutoSeismicCode({code!r}) ok")
            break
        pre_code_parts.append(f"{code}:{msg_pre}")
    pre_code_msg = ("; ".join(pre_code_parts) + "; ") if pre_code_parts else ""

    ok_flat, msg_flat = _try_set_autoseismic_ubc97_flat(sap_model, canon, p, top_s, bot_s)
    if ok_flat:
        return True, f"{pre_code_msg}{msg_flat} (TopStory={top_s!r}, BotStory={bot_s!r}, story_resolve={note})"

    auto = getattr(sap_model.LoadPatterns, "AutoSeismic", None)
    fn = getattr(auto, "SetUBC97", None) if auto is not None else None
    if not callable(fn):
        return False, f"{pre_code_msg}{msg_flat} | AutoSeismic.SetUBC97 not available"

    ret, sig_label = _invoke_autoseismic_set_ubc97_any(fn, canon, p, top_s, bot_s, sap_model)
    if ret is None:
        return False, f"{pre_code_msg}{msg_flat} | AutoSeismic.SetUBC97 raised: {sig_label}"

    # Same convention as SetASCE716: tuple often (..., err); single int use _csi_ret0
    if _autoseismic_setasce716_ret_ok(ret) or _csi_ret0(ret) == 0:
        rb = _ubc97_get_auto_seismic_code(sap_model, canon)
        extra = f" GetAutoSeismicCode={rb!r}" if rb else ""
        return True, (
            f"{pre_code_msg}AutoSeismic.SetUBC97 ok ({sig_label}){extra} "
            f"(TopStory={top_s!r}, BotStory={bot_s!r}, story_resolve={note})"
        )

    for code in ("UBC 97", "1997 UBC", "UBC97"):
        ok_c, msg_c = _try_set_auto_seismic_code(sap_model, canon, code)
        if ok_c:
            rb = _ubc97_get_auto_seismic_code(sap_model, canon)
            return True, f"{pre_code_msg}SetAutoSeismicCode({code!r}) after SetUBC97 fail; readback={rb!r}"
    sfx = ""
    try:
        if int(_csi_ret0(ret)) == -99:
            sfx = " [CSI -99: invalid/unimplemented for this model or ETABS build; check OAPI CHM SetUBC97 / try GUI once]"
    except (TypeError, ValueError):
        pass
    return False, (
        f"{pre_code_msg}{msg_flat} | AutoSeismic.SetUBC97 ret={ret!r} "
        f"(TopStory={top_s!r}, BotStory={bot_s!r}, story_resolve={note}){sfx}"
    )


def apply_ubc97_auto_seismic_patterns(
    sap_model,
    *,
    seismic_x: str,
    seismic_y: str,
    params: Ubc97SeismicParams,
) -> Dict[str, Any]:
    """Create QX/QY (or custom names) Quake patterns and apply 1997 UBC auto-seismic parameters."""
    log: Dict[str, Any] = {"steps": [], "auto_lateral_ok": True}
    for nm, direction in ((str(seismic_x), 1), (str(seismic_y), 2)):
        p = replace(params, direction=int(direction))
        canon = _resolve_pattern_label(sap_model, nm)
        ok, msg = _ensure_load_pattern(sap_model, canon, LTYPE_SEISMIC, 0.0)
        log["steps"].append({f"seismic_add:{canon}": msg})
        if not ok:
            log["auto_lateral_ok"] = False
            log["steps"].append({f"seismic_auto:{canon}": f"skip_add_failed:{msg}"})
            continue
        ok2, msg2 = _try_set_autoseismic_ubc97(sap_model, canon, p)
        log["steps"].append({f"seismic_auto:{canon}": msg2 if ok2 else f"WARN:{msg2}"})
        if not ok2:
            log["auto_lateral_ok"] = False
    log["ok"] = True
    return log


def seismic_asce716_defaults_x() -> Asce716SeismicASCE716Params:
    """Preset for global-X seismic pattern (edit Ss, S1, R, site_class, etc.)."""
    return Asce716SeismicASCE716Params(direction=1)


def seismic_asce716_defaults_y() -> Asce716SeismicASCE716Params:
    """Preset for global-Y seismic pattern."""
    return Asce716SeismicASCE716Params(direction=2)


def _resolve_pattern_label(sap_model, desired: str) -> str:
    """If ``desired`` matches an existing pattern ignoring case (e.g. DEAD vs Dead), return the model name."""
    dl = str(desired).lower()
    for n in _pattern_names(sap_model):
        if str(n).lower() == dl:
            return str(n)
    return str(desired)


def _align_cfg_pattern_names(sap_model, cfg) -> None:
    """
    ETABS ``File.NewBlank`` often includes ``Dead`` / ``Live``; user config may use
    ``DEAD`` / ``LIVE``. Point ``cfg`` at the names ETABS already has so
    ``LoadPatterns.Add`` is not called for a duplicate.
    """
    for field in (
        "dead",
        "live",
        "super_dead",
        "special_lateral",
        "seismic_x",
        "seismic_y",
        "wind",
    ):
        cur = getattr(cfg, field)
        canon = _resolve_pattern_label(sap_model, cur)
        if canon != cur:
            setattr(cfg, field, canon)


def _pattern_names(sap_model) -> List[str]:
    """
    CSI ``GetNameList`` often returns ``(n, names)``; some COM bridges return a
    single sequence. Collect the longest string-like list so existing patterns
    (e.g. DEAD added before this module runs) are visible to ``_ensure_load_pattern``.
    """
    try:
        raw = sap_model.LoadPatterns.GetNameList()
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            return []
        candidates: List[List[str]] = []
        for idx in range(len(raw)):
            if raw[idx] is None:
                continue
            v = raw[idx]
            if isinstance(v, str):
                candidates.append([v])
            elif hasattr(v, "__iter__") and not isinstance(v, (bytes, str)):
                try:
                    candidates.append([str(x) for x in v])
                except (TypeError, ValueError):
                    continue
        if not candidates:
            return []
        return max(candidates, key=len)
    except (TypeError, IndexError, AttributeError, ValueError):
        return []


def _csi_ret0(val: Any) -> int:
    """CSI/COM helpers often return ``int`` or ``[ret, ...]``; use first element as status (0 = ok)."""
    if val is None:
        return 0
    if isinstance(val, (list, tuple)):
        if len(val) == 0:
            return 0
        try:
            return int(val[0])
        except (TypeError, ValueError):
            return -1
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


def _autowind_setasce716_ret_ok(val: Any) -> bool:
    """
    ``cAutoWind.SetASCE716`` returns a status int (0 = ok in typical CSI builds, ``-99`` =
    failure / not applied). Unwrap single-element lists from some comtypes return paths.
    """
    if val is None:
        return True
    x: Any = val
    if isinstance(val, (list, tuple)) and len(val) == 1:
        x = val[0]
    try:
        c = int(x)
    except (TypeError, ValueError):
        return False
    return c == 0


def _wind_dir_angle(p: Asce716WindASCE716Params) -> float:
    if p.dir_angle is not None:
        return float(p.dir_angle)
    if p.angle != 0.0:
        return float(p.angle)
    if int(p.direction) == 2:
        return 90.0
    return 0.0


def _wind_speed_for_asce_api(p: Asce716WindASCE716Params) -> float:
    """``SetASCE716`` expects U.S. customary **mph** in typical ASCE 7-16 ETABS builds."""
    v = float(p.wind_speed)
    u = str(getattr(p, "wind_speed_unit", "mph")).lower().replace(" ", "").replace("per", "/")
    if u in ("m_s", "m/s", "ms", "si"):
        return v * _MPS_TO_MPH
    return v


def _params_for_autowind_setasce716(p: Asce716WindASCE716Params) -> List[Any]:
    """Build arguments for ``cAutoWind.SetASCE716`` using the generated CSI typelib order."""
    return [
        int(p.exposure_from),
        float(_wind_dir_angle(p)),
        float(p.cpw),
        float(p.cpl),
        int(p.asce_case),
        float(p.asce_e1),
        float(p.asce_e2),
        bool(p.user_z),
        float(p.top_z),
        float(p.bottom_z),
        float(_wind_speed_for_asce_api(p)),
        int(p.exposure_type),
        float(p.kzt),
        float(p.gust_factor),
        float(p.kd),
        float(p.solid_gross_ratio),
        bool(p.user_exposure),
    ]


@dataclass
class Eurocode2005AutoWindParams:
    """
    Arguments for ``LoadPatterns.AutoWind.SetEurocode12005`` (EN 1991-1-4 style in OAPI).

    ``wind_speed`` here is passed to ETABS in **current velocity units** (often m/s for SI);
    confirm the OAPI / model unit system in the CSI documentation.
    """

    exposure_from: int = 1
    dir_angle: float = 0.0
    cpw: float = 0.8
    cpl: float = -0.5
    user_z: bool = False
    top_z: float = 0.0
    bottom_z: float = 0.0
    wind_speed: float = 25.0
    terrain: int = 0  # OAPI terrain category; confirm in CHM
    orography: float = 1.0
    k1: float = 1.0
    cscd: float = 1.0
    user_exposure: bool = False


def _try_autowind_eurocode_2005(
    sap_model, pattern_name: str, p: Eurocode2005AutoWindParams
) -> Tuple[bool, str]:
    """Call ``cAutoWind.SetEurocode12005`` (not ASCE)."""
    auto = getattr(sap_model.LoadPatterns, "AutoWind", None)
    fn = getattr(auto, "SetEurocode12005", None) if auto is not None else None
    if fn is None:
        return False, "LoadPatterns.AutoWind.SetEurocode12005 not available"
    try:
        ret = fn(
            str(pattern_name),
            int(p.exposure_from),
            float(p.dir_angle),
            float(p.cpw),
            float(p.cpl),
            bool(p.user_z),
            float(p.top_z),
            float(p.bottom_z),
            float(p.wind_speed),
            int(p.terrain),
            float(p.orography),
            float(p.k1),
            float(p.cscd),
            bool(p.user_exposure),
        )
        if _autowind_setasce716_ret_ok(ret):
            return True, "AutoWind.SetEurocode12005 ok"
        return False, f"AutoWind.SetEurocode12005 ret={ret}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _try_set_autowind_subobject_setasce716(
    sap_model, pattern_name: str, p: Asce716WindASCE716Params
) -> Tuple[bool, str]:
    auto = getattr(sap_model.LoadPatterns, "AutoWind", None)
    fn = getattr(auto, "SetASCE716", None) if auto is not None else None
    if fn is None:
        return False, "LoadPatterns.AutoWind.SetASCE716 not available"
    params = _params_for_autowind_setasce716(p)
    try:
        ret = fn(str(pattern_name), *params)
        if not _autowind_setasce716_ret_ok(ret):
            extra = ""
            try:
                ri = int(ret[0] if isinstance(ret, (list, tuple)) and ret else ret)
                if ri == -99:
                    extra = (
                        " | Common causes: wrong load pattern type (use eLoadPatternType_Wind=6, not Quake=5); "
                        "wind speed unit (ASCE API usually expects mph—set wind_speed_unit='m_s' to convert); "
                        "some ETABS builds return -99 for all OAPI AutoWind sets (set in GUI or use manual wind loads)."
                    )
            except (TypeError, ValueError):
                pass
            return False, f"AutoWind.SetASCE716 ret={ret}{extra}"
        return True, "AutoWind.SetASCE716 ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _autoseismic_setasce716_ret_ok(val: Any) -> bool:
    """
    ``cAutoSeismic.SetASCE716`` may return a 2-tuple: ``(nDir, error_code)`` with last value 0 = ok
    (comtypes), or a single int. Do not use ``_csi_ret0`` on the whole value (the first
    element is often six booleans, not a status int).
    """
    if val is None:
        return True
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        try:
            return int(val[1]) == 0
        except (TypeError, ValueError):
            pass
    return _csi_ret0(val) == 0


def _ensure_load_pattern(sap_model, name: str, load_type: int, self_wt: float = 0.0) -> Tuple[bool, str]:
    """Add load pattern if missing. Returns (ok, message). Uses case-insensitive match to ETABS names."""
    canon = _resolve_pattern_label(sap_model, name)
    names = _pattern_names(sap_model)
    lower_map = {str(n).lower(): str(n) for n in names}
    if canon.lower() in lower_map:
        return True, f"pattern_exists:{lower_map[canon.lower()]}"
    ret = sap_model.LoadPatterns.Add(canon, int(load_type), float(self_wt), True)
    if _csi_ret0(ret) != 0:
        # Duplicate name (e.g. already present under case variant) or stale name list
        names2 = _pattern_names(sap_model)
        lower_map2 = {str(n).lower(): str(n) for n in names2}
        if canon.lower() in lower_map2:
            return True, f"pattern_exists_after_add:{lower_map2[canon.lower()]}"
        return False, f"LoadPatterns.Add failed for {canon!r} ret={ret}"
    return True, f"added:{canon}"


def _try_set_auto_seismic_code(sap_model, pattern_name: str, code: str) -> Tuple[bool, str]:
    fn = getattr(sap_model.LoadPatterns, "SetAutoSeismicCode", None)
    if fn is None:
        return False, "SetAutoSeismicCode not available on this API version"
    try:
        ret = fn(pattern_name, code)
        if _csi_ret0(ret) != 0:
            return False, f"SetAutoSeismicCode ret={ret}"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _try_set_auto_wind_code(sap_model, pattern_name: str, code: str) -> Tuple[bool, str]:
    fn = getattr(sap_model.LoadPatterns, "SetAutoWindCode", None)
    if fn is None:
        return False, "SetAutoWindCode not available on this API version"
    try:
        ret = fn(pattern_name, code)
        if _csi_ret0(ret) != 0:
            return False, f"SetAutoWindCode ret={ret}"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _infer_ct_type_from_ct(ct: float) -> int:
    """Map approximate ``ct`` (ft-scale exponent style) to OAPI *CtType* 0..3; default 1 (CSI examples)."""
    c = float(ct)
    if 0.018 <= c <= 0.025:
        return 3
    if 0.012 <= c <= 0.018:
        return 0
    return 1


def _n_dir_api_bools(p: Asce716SeismicASCE716Params) -> Tuple[bool, bool, bool, bool, bool, bool]:
    if p.n_dir is not None:
        vals = [bool(x) for x in p.n_dir]
        if len(vals) == 7:
            vals = vals[1:]
        vals = vals[:6] + [False] * 6
        return tuple(vals[:6])  # type: ignore[return-value]
    # CSI ASCE 7-16 nDir order is interleaved by sign:
    # X, Y, X+Ecc, Y+Ecc, X-Ecc, Y-Ecc.
    # The GUI displays this as two columns, so QX must set slots 0,2,4 and QY slots 1,3,5.
    d = int(p.direction)
    out6 = [False, False, False, False, False, False]
    if d == 1:
        out6[0] = out6[2] = out6[4] = True
    elif d == 2:
        out6[1] = out6[3] = out6[5] = True
    elif 3 <= d <= 6:
        out6[d - 1] = True
    else:
        out6[0] = out6[1] = out6[2] = True
    return (out6[0], out6[1], out6[2], out6[3], out6[4], out6[5])


def _seismic_period_flag_and_user_t(p: Asce716SeismicASCE716Params) -> Tuple[int, float]:
    if p.user_t:
        return 3, float(p.period_t)
    # This ETABS typelib rejects PeriodFlag=0 for ASCE 7-16. Use program-calculated period by default.
    return 1, 0.0


def _site_class_1_to_6_to_api(site_class: int) -> int:
    """1=A … 6=F  →  0..5 as used in many CSI *SiteClass* integer overloads."""
    s = int(site_class) - 1
    if s < 0:
        s = 0
    if s > 5:
        s = 5
    return s


def _try_set_autoseismic_subobject_setasce716(
    sap_model, pattern_name: str, p: Asce716SeismicASCE716Params
) -> Tuple[bool, str]:
    """
    ``cAutoSeismic.SetASCE716`` (e.g. VB)::

        SetASCE716(Name, nDir, Eccen, PeriodFlag, CtType, UserT, UserZ, TopZ, BottomZ,
                   R, Omega, Cd, I, Ss, S1, T_L, SiteClass, Fa, Fv)
    """
    auto = getattr(sap_model.LoadPatterns, "AutoSeismic", None)
    if auto is None:
        return False, "LoadPatterns.AutoSeismic not available"
    # In this generated CSI typelib, SetASCE716_1 is the overload that accepts
    # the normal six-direction nDir array and returns success for ETABS.
    fn = getattr(auto, "SetASCE716_1", None) or getattr(auto, "SetASCE716", None)
    if fn is None:
        return False, "LoadPatterns.AutoSeismic.SetASCE716(_1) not available"
    n_dir = _n_dir_api_bools(p)
    period_flag, user_t = _seismic_period_flag_and_user_t(p)
    ct_type = int(p.ct_type) if p.ct_type is not None else _infer_ct_type_from_ct(p.ct)
    sc = _site_class_1_to_6_to_api(p.site_class)
    try:
        ret = fn(
            str(pattern_name),
            n_dir,
            float(p.eccentricity),
            int(period_flag),
            int(ct_type),
            float(user_t),
            bool(p.user_z),
            float(p.top_z),
            float(p.bottom_z),
            float(p.r),
            float(p.omega),
            float(p.cd),
            float(p.importance),
            float(p.ss),
            float(p.s1),
            float(p.long_period),
            int(sc),
            float(p.fa),
            float(p.fv),
        )
        if not _autoseismic_setasce716_ret_ok(ret):
            return False, f"AutoSeismic.SetASCE716 ret={ret}"
        return True, "AutoSeismic.SetASCE716 ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _try_set_auto_seismic_asce716(
    sap_model, pattern_name: str, p: Asce716SeismicASCE716Params
) -> Tuple[bool, str]:
    err_flat: Optional[str] = None
    fn = getattr(sap_model.LoadPatterns, "SetAutoSeismicASCE716", None)
    if fn is not None:
        try:
            ret = fn(
                str(pattern_name),
                int(p.direction),
                float(p.eccentricity),
                float(p.ct),
                float(p.x_period),
                bool(p.user_z),
                float(p.top_z),
                float(p.bottom_z),
                bool(p.user_t),
                float(p.period_t),
                float(p.user_w),
                float(p.ss),
                float(p.s1),
                float(p.long_period),
                int(p.site_class),
                float(p.fa),
                float(p.fv),
                float(p.r),
                float(p.omega),
                float(p.cd),
                float(p.importance),
                float(p.min_v),
            )
            if _csi_ret0(ret) == 0:
                return True, "SetAutoSeismicASCE716 ok"
            err_flat = f"SetAutoSeismicASCE716 ret={ret}"
        except Exception as exc:  # noqa: BLE001
            err_flat = str(exc)
    ok, msg = _try_set_autoseismic_subobject_setasce716(sap_model, pattern_name, p)
    if ok:
        return True, msg
    if err_flat is not None:
        return False, f"{err_flat} | {msg}"
    return False, msg


def _try_set_auto_wind_asce716(sap_model, pattern_name: str, p: Asce716WindASCE716Params) -> Tuple[bool, str]:
    err_flat: Optional[str] = None
    fn = getattr(sap_model.LoadPatterns, "SetAutoWindASCE716", None)
    if fn is not None:
        try:
            ret = fn(str(pattern_name), *_params_for_autowind_setasce716(p))
            if _csi_ret0(ret) == 0:
                return True, "SetAutoWindASCE716 ok"
            err_flat = f"SetAutoWindASCE716 ret={ret}"
        except Exception as exc:  # noqa: BLE001
            err_flat = str(exc)
    ok, msg = _try_set_autowind_subobject_setasce716(sap_model, pattern_name, p)
    if ok:
        return True, msg
    if err_flat is not None:
        return False, f"{err_flat} | {msg}"
    return False, msg


def _ensure_wind_load_pattern(
    sap_model, name: str, load_types: Tuple[int, ...] = (LTYPE_WIND, LTYPE_OTHER)
) -> Tuple[bool, str]:
    """Create wind-style pattern; try load types in order until ``Add`` succeeds."""
    if name in _pattern_names(sap_model):
        return True, f"pattern_exists:{name}"
    last_ret = -1
    for lt in load_types:
        ret = sap_model.LoadPatterns.Add(str(name), int(lt), 0.0, True)
        last_ret = _csi_ret0(ret)
        if last_ret == 0:
            return True, f"added:{name} type={lt}"
    return False, f"LoadPatterns.Add failed for {name!r} last_ret={last_ret}"


def _area_names(sap_model) -> List[str]:
    try:
        n, names = sap_model.AreaObj.GetNameList()
        if int(n) <= 0:
            return []
        return [str(x) for x in names]
    except (TypeError, ValueError, AttributeError):
        return []


def _set_area_uniform(
    sap_model,
    area_name: str,
    load_pattern: str,
    pressure: float,
    *,
    direction: int = 6,
) -> int:
    """
    Uniform area surface load (present force/area units).
    With global Z up, a **negative** value often acts downward for ``direction=6`` (match ``etabs_api.area``).
    """
    try:
        from etabs_api import EtabsLoading  # same logic as structured_analysis_export

        ok = EtabsLoading(sap_model).assign_area_uniform_load(
            str(area_name), str(load_pattern), float(pressure), int(direction)
        )
        return 0 if ok else -1
    except Exception:
        return -1


@dataclass
class Asce716LoadConfig:
    """User-facing configuration (pattern names + surface intensities + options)."""

    # Pattern names (must be unique in the model)
    dead: str = "DEAD"
    live: str = "LIVE"
    super_dead: str = "SDL"
    special_lateral: str = "LAT_OTHER"  # Optional LTYPE_OTHER - manual / future automation
    seismic_x: str = "QX"
    seismic_y: str = "QY"
    wind: str = "WIND"

    # Uniform surface loads applied to **slab areas** (present units: force/length²)
    dead_uniform: float = 0.0
    live_uniform: float = 0.0
    super_dead_uniform: float = 0.0

    # Self-weight: only for dead-type pattern that should include DEAD element self-mass
    dead_self_weight_multiplier: float = 1.0

    asce_edition: str = "ASCE 7-16"

    include_wind: bool = True
    include_seismic: bool = True
    include_special_lateral: bool = False

    # Which preset combination packs to add
    design_sets: Tuple[str, ...] = ("concrete_frame", "steel_frame", "slab_gravity")

    # Area load direction: 6 = Global Z (see ETABS docs); 10 = gravity-type in some builds
    area_load_direction: int = 6

    # If None, assign all areas; else restrict to these names
    area_names: Optional[List[str]] = None

    # ASCE 7-16 auto lateral (linear static): pass to call SetAutoSeismicASCE716 / SetAutoWindASCE716
    seismic_x_params: Optional[Asce716SeismicASCE716Params] = None
    seismic_y_params: Optional[Asce716SeismicASCE716Params] = None
    wind_params: Optional[Asce716WindASCE716Params] = None
    # If ASCE ``SetASCE716`` fails (e.g. ret -99), optionally try ``AutoWind.SetEurocode12005``
    try_eurocode_wind_if_asce_fails: bool = False
    eurocode_wind_params: Optional[Eurocode2005AutoWindParams] = None
    # Try these ``LoadPatterns.Add`` types in order until one succeeds for wind
    wind_pattern_load_types: Tuple[int, ...] = (LTYPE_WIND, LTYPE_OTHER)


DEFAULT_ASCE716_DESIGN_SETS: Tuple[str, ...] = ("concrete_frame", "steel_frame", "slab_gravity")


def should_apply_template_combos(raw: Optional[Dict[str, Any]]) -> bool:
    """
    Whether preset ``CONC_*`` / ``STL_*`` / ``SLAB_*`` packs from ``design_sets`` should run.

    - If ``template_combos`` is present in JSON, its boolean value wins.
    - Otherwise falls back to legacy ``apply`` (same meaning as before ``custom_combos`` existed).
    """
    if not raw or not isinstance(raw, dict):
        return False
    if "template_combos" in raw:
        return bool(raw["template_combos"])
    return bool(raw.get("apply", False))


def should_run_asce716_combo_section(raw: Optional[Dict[str, Any]]) -> bool:
    """True if template packs and/or ``custom_combos`` should be processed."""
    if not raw or not isinstance(raw, dict):
        return False
    if should_apply_template_combos(raw):
        return True
    if bool(raw.get("use_etabs_default_combos", False)):
        return True
    return bool(parse_custom_combos_from_dict(raw))


def _parse_combo_terms_list(terms: Any) -> Optional[List[Tuple[str, float]]]:
    """Parse JSON terms: ``[[\"DEAD\", 1.4], ...]`` or ``[{\"pattern\": \"D\", \"factor\": 1.4}, ...]``."""
    if not isinstance(terms, (list, tuple)):
        return None
    out: List[Tuple[str, float]] = []
    for row in terms:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append((str(row[0]), float(row[1])))
        elif isinstance(row, dict) and "pattern" in row:
            out.append((str(row["pattern"]), float(row.get("factor", 1.0))))
    return out if out else None


def parse_custom_combos_from_dict(raw: Optional[Dict[str, Any]]) -> Dict[str, List[Tuple[str, float]]]:
    """
    Parse ``custom_combos`` from JSON.

    **Form A — object (recommended)**::

        "custom_combos": {
          "MY_ULS": [["DEAD", 1.4], ["LIVE", 1.6]],
          "MY_SLS": [["DEAD", 1.0], ["LIVE", 1.0]]
        }

    **Form B — array**::

        "custom_combos": [
          {"name": "MY_ULS", "terms": [["DEAD", 1.4], ["LIVE", 1.6]]}
        ]
    """
    if not raw or not isinstance(raw, dict):
        return {}
    cc = raw.get("custom_combos")
    if cc is None:
        return {}
    out: Dict[str, List[Tuple[str, float]]] = {}
    if isinstance(cc, dict):
        for name, terms in cc.items():
            nm = str(name).strip()
            if not nm:
                continue
            parsed = _parse_combo_terms_list(terms)
            if parsed:
                out[nm] = parsed
        return out
    if isinstance(cc, list):
        for item in cc:
            if not isinstance(item, dict):
                continue
            nm = str(item.get("name", "")).strip()
            parsed = _parse_combo_terms_list(item.get("terms"))
            if nm and parsed:
                out[nm] = parsed
        return out
    return {}


def apply_custom_response_combinations(
    sap_model,
    definitions: Dict[str, List[Tuple[str, float]]],
    *,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    """
    Add linear-add response combinations from explicit ``(pattern, factor)`` lists.

    If ``replace_existing`` is True, calls ``RespCombo.Delete`` before ``Add`` for each name
    (best-effort delete if the combo already exists).
    """
    log: Dict[str, Any] = {"combos": [], "ok": True}
    existing = _existing_patterns(sap_model)
    for name, terms in definitions.items():
        nm = str(name).strip()
        if not nm:
            continue
        filtered = _filter_combo_terms(list(terms), existing)
        if filtered is None:
            log["combos"].append({"name": nm, "skipped": True, "reason": "missing load pattern"})
            continue
        if replace_existing:
            try:
                sap_model.RespCombo.Delete(nm)
            except Exception:
                pass
        ok, msg = _add_response_combo(sap_model, nm, filtered)
        log["combos"].append({"name": nm, "ok": ok, "detail": msg, "custom": True})
        if not ok:
            log["ok"] = False
    return log


def asce716_load_config_from_analysis_inputs(
    raw: Optional[Dict[str, Any]],
    *,
    dead: str,
    live: str,
    super_dead: str,
    seismic_x: str,
    seismic_y: str,
    wind: str = "WIND",
) -> Optional[Asce716LoadConfig]:
    """
    Build an :class:`Asce716LoadConfig` for **response combinations only** (see
    :func:`apply_response_combinations`) from a JSON ``analysis_inputs`` dict.

    Expected shape::

        {
          "apply": true,
          "template_combos": true,
          "design_sets": ["concrete_frame", "slab_gravity"],
          "include_wind": false,
          "dead": "DEAD",
          "live": "LIVE",
          "super_dead": "SDL",
          "wind": "WIND",
          "include_seismic": true,
          "custom_combos": { "MY_ULS": [["DEAD", 1.4], ["LIVE", 1.6]] },
          "replace_existing_combos": false
        }

    If ``raw`` is missing, not a dict, or :func:`should_apply_template_combos` is false,
    returns ``None`` (preset packs skipped; you may still use ``custom_combos`` alone).

    Pattern names default to the caller's ``dead`` / ``live`` / ``super_dead`` /
    ``seismic_*`` so they stay aligned with ``structured_analysis_export`` slab loads
    and auto seismic; JSON keys override when present.
    """
    if not raw or not isinstance(raw, dict):
        return None
    if not should_apply_template_combos(raw):
        return None

    ds_raw = raw.get("design_sets")
    if isinstance(ds_raw, (list, tuple)) and len(ds_raw) > 0:
        design_sets: Tuple[str, ...] = tuple(str(x) for x in ds_raw)
    else:
        design_sets = DEFAULT_ASCE716_DESIGN_SETS

    inc_wind = bool(raw.get("include_wind", False))
    inc_seismic = bool(raw.get("include_seismic", True))

    return Asce716LoadConfig(
        dead=str(raw.get("dead", dead)),
        live=str(raw.get("live", live)),
        super_dead=str(raw.get("super_dead", super_dead)),
        seismic_x=str(raw.get("seismic_x", seismic_x)),
        seismic_y=str(raw.get("seismic_y", seismic_y)),
        wind=str(raw.get("wind", wind)),
        design_sets=design_sets,
        include_wind=inc_wind,
        include_seismic=inc_seismic,
        include_special_lateral=False,
        dead_uniform=0.0,
        live_uniform=0.0,
        super_dead_uniform=0.0,
        seismic_x_params=None,
        seismic_y_params=None,
        wind_params=None,
    )


def ensure_wind_load_pattern_exists(sap_model, wind_name: str) -> Tuple[bool, str]:
    """Create an empty wind-style pattern if missing (so wind terms in combos resolve)."""
    return _ensure_wind_load_pattern(sap_model, str(wind_name), (LTYPE_WIND, LTYPE_OTHER))


def _add_response_combo(
    sap_model,
    combo_name: str,
    terms: Sequence[Tuple[str, float]],
) -> Tuple[bool, str]:
    """terms: (load_pattern_or_case_name, factor)."""
    existing = set()
    try:
        existing = set(sap_model.RespCombo.GetNameList()[1])
    except (TypeError, IndexError, AttributeError):
        pass
    if combo_name in existing:
        return True, f"combo_exists:{combo_name}"
    ret = sap_model.RespCombo.Add(str(combo_name), 0)
    if _csi_ret0(ret) != 0:
        return False, f"RespCombo.Add {combo_name} ret={ret}"
    for pat, fact in terms:
        r2 = sap_model.RespCombo.SetCaseList(str(combo_name), 0, str(pat), float(fact))
        if _csi_ret0(r2) != 0:
            return False, f"SetCaseList {combo_name}+{pat} ret={r2}"
    return True, "ok"


def _combo_templates(cfg: Asce716LoadConfig) -> Dict[str, List[Tuple[str, float]]]:
    """Simplified LRFD-style gravity / companion templates (verify per project)."""
    D, L, Sd, Wx, Wy = cfg.dead, cfg.live, cfg.super_dead, cfg.seismic_x, cfg.seismic_y
    W = cfg.wind
    out: Dict[str, List[Tuple[str, float]]] = {}

    # --- Slab / gravity checks (common superposition) ---
    out["SLAB_1.4D"] = [(D, 1.4)]
    out["SLAB_1.2D_1.6L"] = [(D, 1.2), (L, 1.6)]
    out["SLAB_1.2D_1.6L_0.25Sd"] = [(D, 1.2), (L, 1.6), (Sd, 0.25)]

    # --- Concrete frame (illustrative strength companions) ---
    out["CONC_1.4D"] = [(D, 1.4)]
    out["CONC_1.2D_1.6L"] = [(D, 1.2), (L, 1.6)]
    out["CONC_1.2D_1.0L_1.0Qx"] = [(D, 1.2), (L, 1.0), (Wx, 1.0)]
    out["CONC_1.2D_1.0L_1.0Qy"] = [(D, 1.2), (L, 1.0), (Wy, 1.0)]
    out["CONC_1.2D_1.0W"] = [(D, 1.2), (W, 1.0)]
    out["CONC_0.9D_1.0W"] = [(D, 0.9), (W, 1.0)]

    # --- Steel frame (same skeleton; AISC combinations often mirror ASCE load cases) ---
    out["STL_1.4D"] = [(D, 1.4)]
    out["STL_1.2D_1.6L"] = [(D, 1.2), (L, 1.6)]
    out["STL_1.2D_1.0L_1.0Qx"] = [(D, 1.2), (L, 1.0), (Wx, 1.0)]
    out["STL_1.2D_1.0L_1.0Qy"] = [(D, 1.2), (L, 1.0), (Wy, 1.0)]
    out["STL_1.2D_1.0W"] = [(D, 1.2), (W, 1.0)]

    return out


def apply_load_patterns(sap_model, cfg: Asce716LoadConfig) -> Dict[str, Any]:
    """Create DEAD (with optional self-weight), LIVE, SDL, optional seismic/wind/other."""
    log: Dict[str, Any] = {"steps": [], "auto_lateral_ok": True}
    _align_cfg_pattern_names(sap_model, cfg)

    ok, msg = _ensure_load_pattern(sap_model, cfg.dead, LTYPE_DEAD, cfg.dead_self_weight_multiplier)
    log["steps"].append({"dead": msg})
    if not ok:
        return log

    ok, msg = _ensure_load_pattern(sap_model, cfg.live, LTYPE_LIVE, 0.0)
    log["steps"].append({"live": msg})
    if not ok:
        return log

    ok, msg = _ensure_load_pattern(sap_model, cfg.super_dead, LTYPE_SUPER_DEAD, 0.0)
    log["steps"].append({"super_dead": msg})
    if not ok:
        return log

    if cfg.include_special_lateral:
        ok, msg = _ensure_load_pattern(sap_model, cfg.special_lateral, LTYPE_OTHER, 0.0)
        log["steps"].append({"special_lateral": msg})

    if cfg.include_seismic:
        seismic_specs: List[Tuple[str, Optional[Asce716SeismicASCE716Params]]] = [
            (cfg.seismic_x, cfg.seismic_x_params),
            (cfg.seismic_y, cfg.seismic_y_params),
        ]
        for nm, sp in seismic_specs:
            ok, msg = _ensure_load_pattern(sap_model, nm, LTYPE_SEISMIC, 0.0)
            log["steps"].append({f"seismic_add:{nm}": msg})
            if not ok:
                continue
            ok2, msg2 = False, ""
            if sp is not None:
                ok2, msg2 = _try_set_auto_seismic_asce716(sap_model, nm, sp)
            if not ok2:
                ok_fb, msg_fb = _try_set_auto_seismic_code(sap_model, nm, cfg.asce_edition)
                msg2 = (
                    f"{msg2} | fallback SetAutoSeismicCode: {msg_fb}"
                    if sp is not None
                    else msg_fb
                )
                ok2 = ok_fb
            if sp is not None and not ok2:
                log["auto_lateral_ok"] = False
            log["steps"].append({f"seismic_auto:{nm}": msg2 if ok2 else f"WARN:{msg2}"})

    if cfg.include_wind:
        ok, msg = _ensure_wind_load_pattern(sap_model, cfg.wind, cfg.wind_pattern_load_types)
        log["steps"].append({"wind_add": msg})
        if ok:
            ok2, msg2 = False, ""
            if cfg.wind_params is not None:
                ok2, msg2 = _try_set_auto_wind_asce716(sap_model, cfg.wind, cfg.wind_params)
            if (not ok2) and bool(getattr(cfg, "try_eurocode_wind_if_asce_fails", False)):
                ep = cfg.eurocode_wind_params or Eurocode2005AutoWindParams()
                ok_eu, msg_eu = _try_autowind_eurocode_2005(sap_model, cfg.wind, ep)
                if ok_eu:
                    ok2, msg2 = True, f"AutoWind: ASCE path failed ({msg2}); Eurocode2005 ok ({msg_eu})"
                else:
                    msg2 = f"{msg2} | Eurocode2005 try: {msg_eu}"
            if not ok2:
                ok_fb, msg_fb = _try_set_auto_wind_code(sap_model, cfg.wind, cfg.asce_edition)
                msg2 = (
                    f"{msg2} | fallback SetAutoWindCode: {msg_fb}"
                    if cfg.wind_params is not None
                    else msg_fb
                )
                ok2 = ok_fb
            if cfg.wind_params is not None and not ok2:
                log["auto_lateral_ok"] = False
            log["steps"].append({"wind_auto": msg2 if ok2 else f"WARN:{msg2}"})

    log["ok"] = True
    return log


def _existing_patterns(sap_model) -> set:
    return set(_pattern_names(sap_model))


def _filter_combo_terms(terms: List[Tuple[str, float]], existing: set) -> Optional[List[Tuple[str, float]]]:
    """Return terms if all patterns exist; else None."""
    for pat, _ in terms:
        if pat not in existing:
            return None
    return terms


def apply_response_combinations(sap_model, cfg: Asce716LoadConfig) -> Dict[str, Any]:
    """Add template combos selected by ``cfg.design_sets`` (skips any combo with missing patterns)."""
    log: Dict[str, Any] = {"combos": [], "ok": True}
    templates = _combo_templates(cfg)
    existing = _existing_patterns(sap_model)
    keys_by_set: Dict[str, Tuple[str, ...]] = {
        "slab_gravity": ("SLAB_1.4D", "SLAB_1.2D_1.6L", "SLAB_1.2D_1.6L_0.25Sd"),
        "concrete_frame": (
            "CONC_1.4D",
            "CONC_1.2D_1.6L",
            "CONC_1.2D_1.0L_1.0Qx",
            "CONC_1.2D_1.0L_1.0Qy",
            "CONC_1.2D_1.0W",
            "CONC_0.9D_1.0W",
        ),
        "steel_frame": (
            "STL_1.4D",
            "STL_1.2D_1.6L",
            "STL_1.2D_1.0L_1.0Qx",
            "STL_1.2D_1.0L_1.0Qy",
            "STL_1.2D_1.0W",
        ),
    }
    for dset in cfg.design_sets:
        combo_keys = keys_by_set.get(str(dset))
        if not combo_keys:
            log["combos"].append({"skip": dset, "reason": "unknown design set"})
            continue
        for ck in combo_keys:
            raw = templates.get(ck)
            if not raw:
                continue
            terms = _filter_combo_terms(list(raw), existing)
            if terms is None:
                log["combos"].append({"name": ck, "skipped": True, "reason": "missing load pattern"})
                continue
            ok, msg = _add_response_combo(sap_model, ck, terms)
            log["combos"].append({"name": ck, "ok": ok, "detail": msg})
            if not ok:
                log["ok"] = False
    return log


def assign_uniform_loads_to_slabs(sap_model, cfg: Asce716LoadConfig) -> Dict[str, Any]:
    """
    Apply uniform **surface** load (present units) on each area.

    For downward gravity-style pressure in many models, use a **negative** value if
    global +Z is up and direction uses global Z; with ``direction=10`` (gravity),
    ETABS often expects positive as downward gravity load magnitude - **check**
    one bay in the GUI after first run.
    """
    areas = cfg.area_names if cfg.area_names is not None else _area_names(sap_model)
    log: Dict[str, Any] = {"areas": len(areas), "assigned": [], "errors": []}

    assignments: List[Tuple[str, str, float]] = []
    if cfg.dead_uniform != 0.0:
        assignments.extend((a, cfg.dead, cfg.dead_uniform) for a in areas)
    if cfg.live_uniform != 0.0:
        assignments.extend((a, cfg.live, cfg.live_uniform) for a in areas)
    if cfg.super_dead_uniform != 0.0:
        assignments.extend((a, cfg.super_dead, cfg.super_dead_uniform) for a in areas)

    for area, pat, w in assignments:
        ret = _set_area_uniform(sap_model, area, pat, w, direction=cfg.area_load_direction)
        if ret == 0:
            log["assigned"].append({"area": area, "pattern": pat, "w": w})
        else:
            log["errors"].append({"area": area, "pattern": pat, "ret": ret})
    log["ok"] = len(log["errors"]) == 0
    return log


def setup_asce716_loads(sap_model, cfg: Asce716LoadConfig) -> Dict[str, Any]:
    """Run patterns, combos, and slab loads; returns a single report dict."""
    report: Dict[str, Any] = {"patterns": {}, "combos": {}, "slabs": {}}
    report["patterns"] = apply_load_patterns(sap_model, cfg)
    if not report["patterns"].get("ok"):
        return report
    report["combos"] = apply_response_combinations(sap_model, cfg)
    report["slabs"] = assign_uniform_loads_to_slabs(sap_model, cfg)
    report["ok"] = bool(
        report["patterns"].get("ok")
        and report["patterns"].get("auto_lateral_ok", True)
        and report["combos"].get("ok")
        and report["slabs"].get("ok")
    )
    return report


def lateral_load_guidance() -> str:
    """Short engineering note for callers / docs."""
    return (
        "Lateral loads in ETABS (this module):\n"
        "1) **Wind** - Load pattern type must be **Wind (6)**, not Quake (5). Create pattern (type 6 then 8), "
        "then pass ``wind_params``; the module uses ``SetAutoWindASCE716`` or "
        "``LoadPatterns.AutoWind.SetASCE716``, else ``SetAutoWindCode``. ASCE autowind speed is usually "
        "**mph**; use ``wind_speed_unit='m_s'`` in ``Asce716WindASCE716Params`` to convert from m/s. "
        "Optional: ``try_eurocode_wind_if_asce_fails`` + ``Eurocode2005AutoWindParams`` for "
        "``SetEurocode12005``. If the API returns **-99**, some builds require defining auto wind in the "
        "ETABS GUI or using manual shell/frame loads.\n"
        "2) **Seismic** - Create Quake patterns ``QX``/``QY``, then pass ``seismic_x_params`` / "
        "``seismic_y_params``; the module uses ``SetAutoSeismicASCE716`` or "
        "``LoadPatterns.AutoSeismic.SetASCE716`` (``nDir`` + ASCE7-16 fields), else "
        "``SetAutoSeismicCode``. Default ``direction=1`` (QX) turns on **X, X+Ecc, X−Ecc**; "
        "``direction=2`` (QY) turns on **Y, Y+Ecc, Y−Ecc**, matching the ASCE 7-16 dialog.\n"
        "3) **Special lateral** - optional ``LAT_OTHER`` is disabled by default; enable "
        "``include_special_lateral`` only when you need user story forces, pushover, notional loads, etc.\n"
        "4) **Combinations** - templates are examples; verify against ASCE 7-16 Sec. 2.3 and ACI/AISC.\n"
        "5) **OAPI** - many builds expose ASCE 7-16 auto lateral via "
        "``LoadPatterns.AutoSeismic.SetASCE716`` / ``LoadPatterns.AutoWind.SetASCE716`` "
        "rather than flat ``SetAutoSeismicASCE716`` / ``SetAutoWindASCE716``; confirm in "
        "the CHM for your ETABS version. COM methods may return ``[ret, ...]``; this "
        "module normalizes those to a single status code where needed."
    )


if __name__ == "__main__":
    print(lateral_load_guidance())
