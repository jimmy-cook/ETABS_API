"""
ETABS present-units (InitializeNewModel) presets for the frame ML builder.

Sampling / ``ResolvedBuilding`` stay in **metres** internally; we map to ETABS
API coordinates via ``length_scale_from_metres`` (multiply all lengths).

Presets align with common ETABS choices and ``etabs_api.etabs_obj.EtabsModel.enum_units``:
  mks  → kN, m, °C   (CSI code 6)
  si   → N, mm, °C   (CSI code 9), scale 1000 (m → mm)
  us   → kip, ft, °F (CSI code 4), scale 1/0.3048 (m → ft)

The frame builder applies **metric** concrete/rebar defaults for codes 6 and 9
(``concrete_fc_mpa`` + MPa-scale rebar) and **US / FPS** defaults for code 4
(``concrete_fc_psi`` + ksi rebar + inch-based cover), unless ``etabs.material_strength_system``
overrides ``auto``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, Optional, Tuple

from .parametric_building_dimensions import ResolvedBuilding

METRES_PER_FOOT = 0.3048

# name -> (InitializeNewModel eUnits code, length multiplier: model_metres * scale -> ETABS length unit)
UNIT_PRESETS: Dict[str, Tuple[int, float]] = {
    "mks": (6, 1.0),
    "metric_mks": (6, 1.0),
    "kn_m": (6, 1.0),
    "si": (9, 1000.0),
    "metric_si": (9, 1000.0),
    "n_mm": (9, 1000.0),
    "us": (4, 1.0 / METRES_PER_FOOT),
    "us_customary": (4, 1.0 / METRES_PER_FOOT),
    "kip_ft": (4, 1.0 / METRES_PER_FOOT),
}


def list_unit_preset_names() -> Tuple[str, ...]:
    """Sorted unique preset keys suitable for argparse choices."""
    return tuple(sorted(set(UNIT_PRESETS.keys())))


def resolve_units(
    preset: Optional[str] = None,
    *,
    present_units_code: Optional[int] = None,
    length_scale_from_metres: Optional[float] = None,
) -> Tuple[int, float]:
    """
    Returns (present_units_code, length_scale_from_metres).

    If ``present_units_code`` is set, it wins; ``length_scale_from_metres`` defaults
    to 1.0 unless explicitly provided (advanced / custom CSI codes).
    """
    if present_units_code is not None:
        scale = 1.0 if length_scale_from_metres is None else float(length_scale_from_metres)
        return int(present_units_code), float(scale)

    key = (preset or "mks").strip().lower()
    if key not in UNIT_PRESETS:
        raise ValueError(
            f"Unknown units preset {preset!r}. Use one of: {', '.join(sorted(UNIT_PRESETS))}."
        )
    code, scale = UNIT_PRESETS[key]
    return int(code), float(scale)


def scale_resolved_building(rb: ResolvedBuilding, scale: float) -> ResolvedBuilding:
    """Scale all length-like fields from metres to ETABS length unit (uniform scale)."""
    s = float(scale)
    if s == 1.0:
        return rb
    s2 = s * s
    s3 = s2 * s
    return replace(
        rb,
        Lx=rb.Lx * s,
        Ly=rb.Ly * s,
        Htotal=rb.Htotal * s,
        Wbase=rb.Wbase * s,
        story_height=rb.story_height * s,
        Sx=rb.Sx * s,
        Sy=rb.Sy * s,
        Smax=rb.Smax * s,
        hb=rb.hb * s,
        bw=rb.bw * s,
        bcx=rb.bcx * s,
        bcy=rb.bcy * s,
        bcx_top=rb.bcx_top * s,
        bcy_top=rb.bcy_top * s,
        bcx_ext=rb.bcx_ext * s,
        bcy_ext=rb.bcy_ext * s,
        ts=rb.ts * s,
        Acol_base=rb.Acol_base * s2,
        Acol_top=rb.Acol_top * s2,
        Acol_ext=rb.Acol_ext * s2,
        Acol_int=rb.Acol_int * s2,
        Vbeam=rb.Vbeam * s3,
        Vslab=rb.Vslab * s3,
        Vcol=rb.Vcol * s3,
        achieved=dict(rb.achieved),
    )
