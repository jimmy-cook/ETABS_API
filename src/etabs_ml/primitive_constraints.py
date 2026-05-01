"""
Engineering-style filters on ResolvedBuilding (after primitives are set).

Bands are inclusive on [lo, hi]. Missing keys in config use wide defaults.

Optional inequality checks (numeric value ``0`` disables that check):

- ``beam_flat_max_ratio``: require ``bw/hb <=`` this value (default ``1`` → no flat beam).
- ``column_side_ratio_max``: require ``max(bcx,bcy)/min(bcx,bcy) <`` this (default ``3``).
- ``beam_onto_column_max_ratio``: require ``bw <= ratio * min(bcx,bcy)`` (default ``1``).
- ``beam_slab_bw_over_ts_min``: require ``bw >= k * ts`` (default ``2.5``).
- ``scwb_stiffness_ratio_min``: require ``I_col/L_col >= k * I_beam/L_beam`` with
  ``I_beam=(1/12)*bw*hb^3``, ``I_col=(1/12)*max(bcx,bcy)*min(bcx,bcy)^3``,
  ``L_col=story_height``, ``L_beam=Smax`` (default ``k=0.5``; crude dataset filter).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .parametric_building_dimensions import ResolvedBuilding


def _in_band(x: float, band: Any) -> bool:
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return True
    lo, hi = float(band[0]), float(band[1])
    return lo <= x <= hi


def validate_building(
    rb: ResolvedBuilding,
    constraints: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    a = rb.achieved

    checks = [
        ("hb_over_Smax", rb.hb / rb.Smax if rb.Smax > 0 else 0.0, constraints.get("hb_over_Smax")),
        ("ts_over_Smax", rb.ts / rb.Smax if rb.Smax > 0 else 0.0, constraints.get("ts_over_Smax")),
        ("plan_aspect_ratio", a["plan_aspect_ratio"], constraints.get("plan_aspect_ratio")),
        ("internal_bay_variance", a["internal_bay_variance"], constraints.get("internal_bay_variance")),
        ("elevation_aspect_ratio", a["elevation_aspect_ratio"], constraints.get("elevation_aspect_ratio")),
        ("beam_aspect_ratio", a["beam_aspect_ratio"], constraints.get("beam_aspect_ratio")),
        ("column_aspect_ratio", a["column_aspect_ratio"], constraints.get("column_aspect_ratio")),
        ("column_area_index", a["column_area_index"], constraints.get("column_area_index")),
        ("ext_to_int_col_area_ratio", a["ext_to_int_col_area_ratio"], constraints.get("ext_to_int_col_area_ratio")),
        ("beam_to_slab_vol_ratio", a["beam_to_slab_vol_ratio"], constraints.get("beam_to_slab_vol_ratio")),
        ("col_vol_to_floor_vol", a["col_vol_to_floor_vol"], constraints.get("col_vol_to_floor_vol")),
    ]
    for name, val, band in checks:
        if band is not None and not _in_band(val, band):
            reasons.append(f"{name}={val:.4g} outside {band}")

    # Absolute geometry / member size
    if not _in_band(rb.Smax, constraints.get("Smax_m")):
        reasons.append(f"Smax={rb.Smax:.4g} outside {constraints.get('Smax_m')}")
    if not _in_band(rb.story_height, constraints.get("h_story_m")):
        reasons.append(f"h_story={rb.story_height:.4g} outside {constraints.get('h_story_m')}")
    if not _in_band(rb.hb, constraints.get("hb_m")):
        reasons.append(f"hb={rb.hb:.4g} outside {constraints.get('hb_m')}")
    if not _in_band(rb.bw, constraints.get("bw_m")):
        reasons.append(f"bw={rb.bw:.4g} outside {constraints.get('bw_m')}")
    cmin = min(rb.bcx, rb.bcy)
    if not _in_band(cmin, constraints.get("bc_min_m")):
        reasons.append(f"min(bcx,bcy)={cmin:.4g} outside {constraints.get('bc_min_m')}")
    if not _in_band(rb.ts, constraints.get("ts_m_abs")):
        reasons.append(f"ts={rb.ts:.4g} outside {constraints.get('ts_m_abs')}")

    fac = float(constraints.get("column_vs_beam_factor", 0.0) or 0.0)
    if fac > 0 and rb.hb > 0 and cmin < fac * rb.hb:
        reasons.append(f"column_vs_beam: min_col={cmin:.4g} < {fac}*hb={fac * rb.hb:.4g}")

    smax_sl = float(constraints.get("column_slenderness_max", 0.0) or 0.0)
    if smax_sl > 0 and cmin > 0:
        sl = rb.story_height / cmin
        if sl > smax_sl:
            reasons.append(f"column_slenderness={sl:.2f} > {smax_sl}")

    # --- Explicit constructability / stiffness (set key to 0 to disable) ---
    flat_max = float(constraints.get("beam_flat_max_ratio", 1.0) or 0.0)
    if flat_max > 0 and rb.hb > 0:
        r_wh = rb.bw / rb.hb
        if r_wh > flat_max:
            reasons.append(f"beam_flat: bw/hb={r_wh:.4g} > {flat_max}")

    col_side_max = float(constraints.get("column_side_ratio_max", 3.0) or 0.0)
    if col_side_max > 0 and cmin > 0:
        cmax = max(rb.bcx, rb.bcy)
        elong = cmax / cmin
        if elong >= col_side_max:
            reasons.append(f"column_elongation: max/min={elong:.4g} >= {col_side_max}")

    onto = float(constraints.get("beam_onto_column_max_ratio", 1.0) or 0.0)
    if onto > 0 and cmin > 0 and rb.bw > onto * cmin:
        reasons.append(f"beam_onto_column: bw={rb.bw:.4g} > {onto}*min_col={onto * cmin:.4g}")

    bw_ts = float(constraints.get("beam_slab_bw_over_ts_min", 2.5) or 0.0)
    if bw_ts > 0 and rb.ts > 0:
        need_bw = bw_ts * rb.ts
        if rb.bw < need_bw:
            reasons.append(f"beam_slab: bw={rb.bw:.4g} < {bw_ts}*ts={need_bw:.4g}")

    scwb = float(constraints.get("scwb_stiffness_ratio_min", 0.5) or 0.0)
    if scwb > 0 and rb.story_height > 0 and rb.Smax > 0 and rb.hb > 0 and cmin > 0:
        I_beam = (1.0 / 12.0) * rb.bw * (rb.hb**3)
        cmax = max(rb.bcx, rb.bcy)
        I_col = (1.0 / 12.0) * cmax * (cmin**3)
        lhs = I_col / rb.story_height
        rhs = scwb * I_beam / rb.Smax
        if lhs < rhs:
            reasons.append(
                f"scwb: Icol/Lcol={lhs:.4g} < {scwb}*Ibeam/Lbeam={rhs:.4g} "
                f"(Icol={I_col:.4g}, Ibeam={I_beam:.4g})"
            )

    return (len(reasons) == 0, reasons)
