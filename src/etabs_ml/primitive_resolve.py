"""
Build ResolvedBuilding from first-level (primitive) parameters.

Volumes and ratio_* labels match the definitions used in parametric_building_dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from .parametric_building_dimensions import ResolvedBuilding


@dataclass
class PrimitiveSample:
    nx: int
    ny: int
    n_stories: int
    Sx_m: float
    Sy_m: float
    h_story_m: float
    hb_m: float
    bw_m: float
    bcx_m: float
    bcy_m: float
    ts_m: float
    col_top_area_ratio: float
    col_ext_area_ratio: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "nx": self.nx,
            "ny": self.ny,
            "n_stories": self.n_stories,
            "Sx_m": self.Sx_m,
            "Sy_m": self.Sy_m,
            "h_story_m": self.h_story_m,
            "hb_m": self.hb_m,
            "bw_m": self.bw_m,
            "bcx_m": self.bcx_m,
            "bcy_m": self.bcy_m,
            "ts_m": self.ts_m,
            "col_top_area_ratio": self.col_top_area_ratio,
            "col_ext_area_ratio": self.col_ext_area_ratio,
        }


def _order_column_sides(bcx: float, bcy: float) -> Tuple[float, float]:
    """Interior columns: store smaller side as bcx, larger as bcy (consistent with column_aspect_ratio)."""
    a, b = min(bcx, bcy), max(bcx, bcy)
    return a, b


def resolve_from_primitives(p: PrimitiveSample) -> ResolvedBuilding:
    nx, ny = p.nx, p.ny
    Sx, Sy = p.Sx_m, p.Sy_m
    Lx = nx * Sx
    Ly = ny * Sy
    Wbase = max(Lx, Ly)
    n_stories = p.n_stories
    story_height = p.h_story_m
    Htotal = n_stories * story_height
    Smax = max(Sx, Sy)

    hb = p.hb_m
    bw = p.bw_m
    bcx, bcy = _order_column_sides(p.bcx_m, p.bcy_m)
    ts = p.ts_m

    r_top = p.col_top_area_ratio
    r_ext = p.col_ext_area_ratio
    scale_top = r_top**0.5 if r_top > 0 else 1.0
    scale_ext = r_ext**0.5 if r_ext > 0 else 1.0
    bcx_top, bcy_top = bcx * scale_top, bcy * scale_top
    bcx_ext, bcy_ext = bcx * scale_ext, bcy * scale_ext

    n_xj, n_yj = nx + 1, ny + 1
    floor_area = Lx * Ly
    Vslab_one = floor_area * ts
    Vslab = Vslab_one * n_stories

    n_beam_x = ny * nx * n_stories
    n_beam_y = nx * ny * n_stories
    len_beam = Sx * n_beam_x + Sy * n_beam_y
    Vbeam = len_beam * bw * hb

    n_col = n_xj * n_yj * n_stories
    h_col = story_height
    Acol_base = bcx * bcy
    Vcol = n_col * Acol_base * h_col

    Acol_top = bcx_top * bcy_top
    Acol_int = Acol_base
    Acol_ext = bcx_ext * bcy_ext

    achieved = {
        "plan_aspect_ratio": min(Lx, Ly) / max(Lx, Ly) if max(Lx, Ly) > 0 else 0.0,
        "elevation_aspect_ratio": min(Htotal, Wbase) / max(Htotal, Wbase) if max(Htotal, Wbase) > 0 else 0.0,
        "internal_bay_variance": min(Sx, Sy) / max(Sx, Sy) if max(Sx, Sy) > 0 else 0.0,
        "beam_depth_to_span": hb / Smax if Smax > 0 else 0.0,
        "beam_aspect_ratio": bw / hb if hb > 0 else 0.0,
        "column_aspect_ratio": min(bcx, bcy) / max(bcx, bcy) if max(bcx, bcy) > 0 else 0.0,
        "slab_thick_to_span": ts / Smax if Smax > 0 else 0.0,
        "column_area_index": Acol_top / Acol_base if Acol_base > 0 else 0.0,
        "ext_to_int_col_area_ratio": Acol_ext / Acol_int if Acol_int > 0 else 0.0,
        "beam_to_slab_vol_ratio": Vbeam / Vslab if Vslab > 0 else 0.0,
        "col_vol_to_floor_vol": Vcol / (Vbeam + Vslab) if (Vbeam + Vslab) > 0 else 0.0,
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


def flat_record_from_primitive(rb: ResolvedBuilding, prim: PrimitiveSample) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    row.update(prim.as_dict())
    row.update(
        {
            "Lx": rb.Lx,
            "Ly": rb.Ly,
            "Htotal": rb.Htotal,
            "Wbase": rb.Wbase,
            "Smax": rb.Smax,
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
        row[f"ratio_{k}"] = v
    return row
