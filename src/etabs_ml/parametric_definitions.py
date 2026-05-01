"""
Discrete parameter grids for multi-story building dataset generation.

Table fields follow your spec: min, max, increment (documented step),
and variation (number of discrete levels). Levels are generated with
numpy.linspace(min, max, n_levels) so the product of levels matches
factorial-style combinatorics (e.g. total ≈ 4800 when all dimensions multiply).

Non-integer variation (e.g. 2.5) is rounded up to the next integer level count.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence


def _level_count(variation: float) -> int:
    n = int(math.ceil(float(variation)))
    return max(1, n)


def _linspace(min_v: float, max_v: float, variation: float) -> List[float]:
    n = _level_count(variation)
    if n == 1:
        # Single "variation" level: span the min–max interval at mid-range.
        return [0.5 * (float(min_v) + float(max_v))]
    step = (float(max_v) - float(min_v)) / (n - 1)
    return [float(min_v) + i * step for i in range(n)]


@dataclass(frozen=True)
class ParamAxis:
    key: str
    category: str
    expression: str
    min_v: float
    max_v: float
    increment: float  # informational
    variation: float

    def levels(self) -> List[float]:
        return _linspace(self.min_v, self.max_v, self.variation)


# Mirrors the research table (image). Keys are stable for CSV / JSONL metadata.
PARAM_AXES: Sequence[ParamAxis] = (
    ParamAxis(
        "plan_aspect_ratio",
        "Global Geometry",
        "min(Lx, Ly) / max(Lx, Ly)",
        0.3,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "elevation_aspect_ratio",
        "Global Geometry",
        "min(Htotal, Wbase) / max(Htotal, Wbase)",
        0.2,
        1.0,
        0.4,
        3,
    ),
    ParamAxis(
        "internal_bay_variance",
        "Global Geometry",
        "min(Sx, Sy) / max(Sx, Sy)",
        0.5,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "beam_depth_to_span",
        "Section Dimensions",
        "hb / Smax",
        0.07,
        0.12,
        0.04,
        2,
    ),
    ParamAxis(
        "beam_aspect_ratio",
        "Section Dimensions",
        "bw / hb",
        0.4,
        0.8,
        0.4,
        2,
    ),
    ParamAxis(
        "column_aspect_ratio",
        "Section Dimensions",
        "min(bcx, bcy) / max(bcx, bcy)",
        0.5,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "slab_thick_to_span",
        "Volumetric & Area",
        "ts / Smax",
        0.04,
        0.06,
        0.04,
        1,
    ),
    ParamAxis(
        "column_area_index",
        "Volumetric & Area",
        "Acol,top / Acol,base",
        0.3,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "ext_to_int_col_area_ratio",
        "Volumetric & Area",
        "Acol,ext / Acol,int",
        0.5,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "beam_to_slab_vol_ratio",
        "Volumetric & Area",
        "Vbeam / Vslab",
        0.3,
        1.0,
        0.4,
        2,
    ),
    ParamAxis(
        "col_vol_to_floor_vol",
        "Volumetric & Area",
        "Vcol / (Vbeam + Vslab)",
        0.2,
        0.8,
        0.4,
        2.5,
    ),
)


def total_combination_count(axes: Sequence[ParamAxis] = PARAM_AXES) -> int:
    p = 1
    for ax in axes:
        p *= len(ax.levels())
    return p


def iter_param_dicts(axes: Sequence[ParamAxis] = PARAM_AXES) -> Iterator[Dict[str, float]]:
    level_lists = [ax.levels() for ax in axes]
    keys = [ax.key for ax in axes]
    for combo in itertools.product(*level_lists):
        yield dict(zip(keys, combo))


def write_parameter_manifest(path: Path, axes: Sequence[ParamAxis] = PARAM_AXES) -> None:
    payload: Dict[str, Any] = {
        "total_combinations": total_combination_count(axes),
        "axes": [
            {
                "key": ax.key,
                "category": ax.category,
                "expression": ax.expression,
                "min": ax.min_v,
                "max": ax.max_v,
                "increment": ax.increment,
                "variation": ax.variation,
                "n_levels": len(ax.levels()),
                "levels": ax.levels(),
            }
            for ax in axes
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
