"""
Map normalized table parameters to physical quantities (meters) and
analytical volumes / areas for dataset labels.

Resolution strategy (transparent defaults; tune for your research grid):
- Reference plan dimension Ly; Lx = plan_aspect_ratio * Ly with Lx <= Ly.
- Wbase = max(Lx, Ly). Low-rise branch: Htotal = elevation_aspect_ratio * Wbase.
- Integer bays nx, ny chosen from a small search grid to approximate
  internal_bay_variance = min(Sx,Sy)/max(Sx,Sy) with Sx=Lx/nx, Sy=Ly/ny.
- Beam depth hb = beam_depth_to_span * Smax; beam width from aspect.
- Column sides from column_aspect_ratio and a scale chosen from col_vol heuristic.
- Slab thickness ts = slab_thick_to_span * Smax (slabs modeled analytically for volume).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class ResolvedBuilding:
    Lx: float
    Ly: float
    Htotal: float
    Wbase: float
    nx: int
    ny: int
    n_stories: int
    story_height: float
    Sx: float
    Sy: float
    Smax: float
    hb: float
    bw: float
    bcx: float
    bcy: float
    bcx_top: float
    bcy_top: float
    bcx_ext: float
    bcy_ext: float
    ts: float
    Acol_base: float
    Acol_top: float
    Acol_ext: float
    Acol_int: float
    Vbeam: float
    Vslab: float
    Vcol: float
    # achieved ratios (for QC vs targets)
    achieved: Dict[str, float]


def _best_bay_counts(
    Lx: float,
    Ly: float,
    target_bay_var: float,
    nx_max: int = 8,
    ny_max: int = 8,
) -> Tuple[int, int, float]:
    """Pick (nx, ny) minimizing error on internal bay variance."""
    best = (2, 2, 1e9)
    for nx in range(1, nx_max + 1):
        for ny in range(1, ny_max + 1):
            sx = Lx / nx
            sy = Ly / ny
            lo, hi = min(sx, sy), max(sx, sy)
            val = lo / hi if hi > 0 else 1.0
            err = abs(val - target_bay_var)
            if err < best[2]:
                best = (nx, ny, err)
    return best[0], best[1], best[2]


def resolve_building(
    p: Dict[str, float],
    *,
    Ly_ref: float = 30.0,
    n_stories: int = 5,
    elevation_low_rise: bool = True,
    nx_max: int = 8,
    ny_max: int = 8,
) -> ResolvedBuilding:
    plan_ar = p["plan_aspect_ratio"]
    elev_ar = p["elevation_aspect_ratio"]
    bay_var_t = p["internal_bay_variance"]
    hb_over_s = p["beam_depth_to_span"]
    bw_over_hb = p["beam_aspect_ratio"]
    col_ar = p["column_aspect_ratio"]
    ts_over_s = p["slab_thick_to_span"]
    A_top_over_base = p["column_area_index"]
    A_ext_over_int = p["ext_to_int_col_area_ratio"]
    Vb_over_vs_t = p["beam_to_slab_vol_ratio"]
    Vc_over_vf_t = p["col_vol_to_floor_vol"]

    Ly = Ly_ref
    Lx = plan_ar * Ly
    if Lx > Ly:
        Lx, Ly = Ly, Lx

    Wbase = max(Lx, Ly)
    if elevation_low_rise:
        Htotal = elev_ar * Wbase
    else:
        Htotal = Wbase / max(elev_ar, 1e-6)

    story_height = Htotal / max(n_stories, 1)

    nx, ny, _ = _best_bay_counts(Lx, Ly, bay_var_t, nx_max=nx_max, ny_max=ny_max)
    Sx = Lx / nx
    Sy = Ly / ny
    Smax = max(Sx, Sy)

    hb = hb_over_s * Smax

    # Column rectangle with min/max side ratio = col_ar (bcx <= bcy)
    bcy = 0.5 * Smax * 0.12
    bcx = col_ar * bcy

    ts = ts_over_s * Smax

    Acol_base = bcx * bcy
    Acol_top = A_top_over_base * Acol_base
    scale_top = (Acol_top / Acol_base) ** 0.5 if Acol_base > 0 else 1.0
    bcx_top, bcy_top = bcx * scale_top, bcy * scale_top

    Acol_int = Acol_base
    Acol_ext = A_ext_over_int * Acol_int
    scale_ext = (Acol_ext / Acol_int) ** 0.5 if Acol_int > 0 else 1.0
    bcx_ext, bcy_ext = bcx * scale_ext, bcy * scale_ext

    n_xj, n_yj = nx + 1, ny + 1

    floor_area = Lx * Ly
    Vslab_one = floor_area * ts
    Vslab = Vslab_one * n_stories

    n_beam_x = ny * nx * n_stories
    n_beam_y = nx * ny * n_stories
    len_beam = Sx * n_beam_x + Sy * n_beam_y

    n_col = n_xj * n_yj * n_stories
    h_col = story_height
    Vcol_geom = n_col * Acol_base * h_col

    # Beam width: prefer volume target, clamped to table beam aspect ratio band.
    bw = bw_over_hb * hb
    Vbeam_geom = len_beam * bw * hb
    Vb_target = Vb_over_vs_t * Vslab
    bw_min = 0.4 * hb
    bw_max = 0.8 * hb
    if Vbeam_geom > 0 and Vb_target > 0:
        bw_ideal = Vb_target / (len_beam * hb)
        bw = min(bw_max, max(bw_min, bw_ideal))
    Vbeam = len_beam * bw * hb

    V_floor = Vbeam + Vslab
    Vc_target = Vc_over_vf_t * V_floor
    if Vcol_geom > 0 and Vc_target > 0:
        s_c = (Vc_target / Vcol_geom) ** 0.5
        bcx *= max(0.6, min(1.4, s_c))
        bcy *= max(0.6, min(1.4, s_c))
        bcx_top *= max(0.6, min(1.4, s_c))
        bcy_top *= max(0.6, min(1.4, s_c))
        bcx_ext *= max(0.6, min(1.4, s_c))
        bcy_ext *= max(0.6, min(1.4, s_c))
    Acol_base = bcx * bcy
    Acol_top = bcx_top * bcy_top
    Acol_int = bcx * bcy
    Acol_ext = bcx_ext * bcy_ext
    Vcol = n_col * Acol_base * h_col

    achieved = {
        "plan_aspect_ratio": min(Lx, Ly) / max(Lx, Ly),
        "elevation_aspect_ratio": min(Htotal, Wbase) / max(Htotal, Wbase),
        "internal_bay_variance": min(Sx, Sy) / max(Sx, Sy),
        "beam_depth_to_span": hb / Smax,
        "beam_aspect_ratio": bw / hb,
        "column_aspect_ratio": min(bcx, bcy) / max(bcx, bcy),
        "slab_thick_to_span": ts / Smax,
        "column_area_index": Acol_top / Acol_base if Acol_base else 0.0,
        "ext_to_int_col_area_ratio": Acol_ext / Acol_int if Acol_int else 0.0,
        "beam_to_slab_vol_ratio": Vbeam / Vslab if Vslab else 0.0,
        "col_vol_to_floor_vol": Vcol / (Vbeam + Vslab) if (Vbeam + Vslab) else 0.0,
    }

    return ResolvedBuilding(
        Lx=Lx,
        Ly=Ly,
        Htotal=Htotal,
        Wbase=Wbase,
        nx=nx,
        ny=ny,
        n_stories=n_stories,
        story_height=story_height,
        Sx=Sx,
        Sy=Sy,
        Smax=Smax,
        hb=hb,
        bw=bw,
        bcx=bcx,
        bcy=bcy,
        bcx_top=bcx_top,
        bcy_top=bcy_top,
        bcx_ext=bcx_ext,
        bcy_ext=bcy_ext,
        ts=ts,
        Acol_base=Acol_base,
        Acol_top=Acol_top,
        Acol_ext=Acol_ext,
        Acol_int=Acol_int,
        Vbeam=Vbeam,
        Vslab=Vslab,
        Vcol=Vcol,
        achieved=achieved,
    )


def building_to_flat_dict(rb: ResolvedBuilding, p: Dict[str, float]) -> Dict[str, float]:
    row: Dict[str, float] = {f"target_{k}": v for k, v in p.items()}
    row.update(
        {
            "Lx": rb.Lx,
            "Ly": rb.Ly,
            "Htotal": rb.Htotal,
            "Wbase": rb.Wbase,
            "nx": rb.nx,
            "ny": rb.ny,
            "n_stories": rb.n_stories,
            "story_height": rb.story_height,
            "Sx": rb.Sx,
            "Sy": rb.Sy,
            "Smax": rb.Smax,
            "hb": rb.hb,
            "bw": rb.bw,
            "bcx": rb.bcx,
            "bcy": rb.bcy,
            "bcx_top": rb.bcx_top,
            "bcy_top": rb.bcy_top,
            "bcx_ext": rb.bcx_ext,
            "bcy_ext": rb.bcy_ext,
            "ts": rb.ts,
            "Acol_base": rb.Acol_base,
            "Acol_top": rb.Acol_top,
            "Acol_ext": rb.Acol_ext,
            "Acol_int": rb.Acol_int,
            "Vbeam": rb.Vbeam,
            "Vslab": rb.Vslab,
            "Vcol": rb.Vcol,
        }
    )
    for k, v in rb.achieved.items():
        row[f"achieved_{k}"] = v
    return row
