# -*- coding: utf-8 -*-
"""
ETABS multi-story frame — primitive-first data generation (ML building models).

Same *style* as steel connection sample generators (e.g. ShearTabMinorDataGenerator):
  - User edits **lists** and **(min, max)** tuples directly in this class.
  - Flow: define discrete grids + continuous ranges → LHS or random sample
         → engineering constraint filter → save per-model .edb + runs.jsonl + summary.csv

No time-history / linear / nonlinear analysis by default.
Optional post-save response export is available via CLI flags.

Usage:
  python etabs_frame_datagen.py --output ./ml_buildings --no-etabs
  python etabs_frame_datagen.py --output ./ml_buildings --target 100 --seed 2026
  python etabs_frame_datagen.py --output ./ml_us --units us --limit 1

You can also import the class and call ``to_config_dict()`` / ``generate()`` from code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .etabs_units import list_unit_preset_names
from run_primitive_ml_dataset import run_primitive_generation


def _mm(lo: float, hi: float) -> Dict[str, float]:
    return {"min": float(lo), "max": float(hi)}


class EtabsFrameDataGenerator:
    """
    Edit the attributes below (like ``bolt_ds``, ``pitch_values`` in the steel script).

    **Discrete** = only these integers are used (full rotation with continuous draws).
    **Continuous (min, max)** = sampled uniformly within the interval (per LHS stratum).
    **Constraint bands** = inclusive [low, high] checks after a candidate is built.
    """

    def __init__(
        self,
        random_seed: int = 42,
        target_accepted: int = 200,
        max_draws: int = 20000,
        sampling_method: str = "lhs",
        lhs_batch_size: int = 2048,
        etabs_units_preset: str = "mks",
        etabs_present_units: Optional[int] = None,
        etabs_length_scale_from_metres: Optional[float] = None,
    ):
        self.random_seed = random_seed
        self.target_accepted = target_accepted
        self.max_draws = max_draws
        self.sampling_method = sampling_method.lower()
        self.lhs_batch_size = lhs_batch_size
        self.etabs_units_preset = str(etabs_units_preset).strip().lower()
        self.etabs_present_units = etabs_present_units
        self.etabs_length_scale_from_metres = etabs_length_scale_from_metres

        # ----- DISCRETE GRIDS (edit lists) -----
        self.nx_values = [2, 3, 4, 5]  # number of bays in X
        self.ny_values = [2, 3, 4, 5]  # number of bays in Y
        self.n_stories_values = [2, 3, 4, 5, 6, 7, 8, 9, 10]  # above-ground stories
        # Floor-to-floor (m), discrete only: 10 ft, 11 ft, 12 ft (1 ft = 0.3048 m)
        self.h_story_m_discrete: List[float] = [3.048, 3.3528, 3.6576]

        # ----- CONTINUOUS RANGES in meters / ratios (min, max) -----
        # Bay span lengths: Lx = nx * span_x_m, Ly = ny * span_y_m
        self.span_x_m: Tuple[float, float] = (4.0, 9.0)
        self.span_y_m: Tuple[float, float] = (4.0, 9.0)

        # Beam: depth (m); width = beam_width_over_depth * depth
        self.beam_depth_m: Tuple[float, float] = (0.42, 0.95)
        self.beam_width_over_depth: Tuple[float, float] = (0.4, 0.8)

        # Interior column rectangle sides (m); code reorders so smaller = bcx
        self.column_side_x_m: Tuple[float, float] = (0.35, 0.65)
        self.column_side_y_m: Tuple[float, float] = (0.4, 0.85)

        self.slab_thickness_m: Tuple[float, float] = (0.18, 0.32)

        # Top / exterior column area vs interior base (ratios)
        self.col_top_area_ratio: Tuple[float, float] = (0.35, 1.0)
        self.col_ext_area_ratio: Tuple[float, float] = (0.55, 1.0)

        # ----- ENGINEERING CONSTRAINT BANDS [low, high] (accept / reject) -----
        self.c_hb_over_Smax = (0.06, 0.14)
        self.c_ts_over_Smax = (0.03, 0.08)
        self.c_plan_aspect_ratio = (0.25, 1.0)
        self.c_internal_bay_variance = (0.45, 1.0)
        self.c_elevation_aspect_ratio = (0.15, 1.0)
        self.c_beam_aspect_ratio = (0.35, 0.85)
        self.c_column_aspect_ratio = (0.45, 1.0)
        self.c_column_area_index = (0.25, 1.0)
        self.c_ext_to_int_col_area_ratio = (0.45, 1.0)
        self.c_beam_to_slab_vol_ratio = (0.12, 1.15)
        self.c_col_vol_to_floor_vol = (0.06, 0.95)

        self.c_Smax_m = (3.5, 11.0)
        self.c_h_story_m = (3.048, 3.6576)
        self.c_hb_m = (0.28, 1.05)
        self.c_bw_m = (0.18, 0.75)
        self.c_bc_min_m = (0.3, 0.9)
        self.c_ts_m_abs = (0.12, 0.38)

        # min(bcx,bcy) >= factor * beam_depth
        self.column_vs_beam_factor = 0.62
        self.column_slenderness_max = 28.0

        # primitive_constraints.validate_building inequality checks (0 = off)
        self.beam_flat_max_ratio = 1.0
        self.column_side_ratio_max = 3.0
        self.beam_onto_column_max_ratio = 1.0
        self.beam_slab_bw_over_ts_min = 2.5
        self.scwb_stiffness_ratio_min = 0.5

        # ----- ETABS template -----
        self.material_name = "C40"
        self.beam_material_name = "C40"
        self.column_material_name = "C40"
        self.slab_material_name = "C40"
        self.beam_rebar_long = "A615Gr60"
        self.beam_rebar_tie = "A615Gr60"
        self.column_rebar_long = "A615Gr60"
        self.column_rebar_tie = "A615Gr60"
        self.dead_load_pattern = "DEAD"

        # Written into ``to_config_dict()`` for ``run_primitive_generation`` (post-build pipeline).
        self.with_analysis_responses = True
        self.export_all_available_tables = False

    # --- helpers: tuple -> dict for pipeline ---
    def _cont(self, span: Tuple[float, float]) -> Dict[str, float]:
        return _mm(span[0], span[1])

    def to_config_dict(self) -> Dict[str, Any]:
        """Build the same structure as ``primitive_ml_dataset.default.json``."""
        return {
            "description": "Generated from EtabsFrameDataGenerator (in-code ranges).",
            "sampling": {
                "method": self.sampling_method,
                "target_accepted": int(self.target_accepted),
                "seed": int(self.random_seed),
                "max_draws": int(self.max_draws),
                "lhs_batch_size": int(self.lhs_batch_size),
            },
            "discrete": {
                "nx": list(self.nx_values),
                "ny": list(self.ny_values),
                "n_stories": list(self.n_stories_values),
                "h_story_m": list(self.h_story_m_discrete),
            },
            "continuous": {
                "Sx_m": self._cont(self.span_x_m),
                "Sy_m": self._cont(self.span_y_m),
                "hb_m": self._cont(self.beam_depth_m),
                "bw_over_hb": self._cont(self.beam_width_over_depth),
                "bcx_m": self._cont(self.column_side_x_m),
                "bcy_m": self._cont(self.column_side_y_m),
                "ts_m": self._cont(self.slab_thickness_m),
                "col_top_area_ratio": self._cont(self.col_top_area_ratio),
                "col_ext_area_ratio": self._cont(self.col_ext_area_ratio),
            },
            "constraints": {
                "hb_over_Smax": list(self.c_hb_over_Smax),
                "ts_over_Smax": list(self.c_ts_over_Smax),
                "plan_aspect_ratio": list(self.c_plan_aspect_ratio),
                "internal_bay_variance": list(self.c_internal_bay_variance),
                "elevation_aspect_ratio": list(self.c_elevation_aspect_ratio),
                "beam_aspect_ratio": list(self.c_beam_aspect_ratio),
                "column_aspect_ratio": list(self.c_column_aspect_ratio),
                "column_area_index": list(self.c_column_area_index),
                "ext_to_int_col_area_ratio": list(self.c_ext_to_int_col_area_ratio),
                "beam_to_slab_vol_ratio": list(self.c_beam_to_slab_vol_ratio),
                "col_vol_to_floor_vol": list(self.c_col_vol_to_floor_vol),
                "Smax_m": list(self.c_Smax_m),
                "h_story_m": list(self.c_h_story_m),
                "hb_m": list(self.c_hb_m),
                "bw_m": list(self.c_bw_m),
                "bc_min_m": list(self.c_bc_min_m),
                "ts_m_abs": list(self.c_ts_m_abs),
                "column_vs_beam_factor": float(self.column_vs_beam_factor),
                "column_slenderness_max": float(self.column_slenderness_max),
                "beam_flat_max_ratio": float(self.beam_flat_max_ratio),
                "column_side_ratio_max": float(self.column_side_ratio_max),
                "beam_onto_column_max_ratio": float(self.beam_onto_column_max_ratio),
                "beam_slab_bw_over_ts_min": float(self.beam_slab_bw_over_ts_min),
                "scwb_stiffness_ratio_min": float(self.scwb_stiffness_ratio_min),
            },
            "etabs": self._etabs_config_block(),
            "with_analysis_responses": bool(self.with_analysis_responses),
            "export_all_available_tables": bool(self.export_all_available_tables),
            "analysis_inputs": {
                "super_dead_value": 1.0,
                "live_value": 2.0,
                "seismic_x_name": "QX",
                "seismic_y_name": "QY",
                "seismic_ss": 1.0,
                "seismic_s1": 0.4,
                "seismic_r": 8.0,
                "seismic_site_class": 3,
            },
        }

    def _etabs_config_block(self) -> Dict[str, Any]:
        e: Dict[str, Any] = {
            "material_name": self.material_name,
            "beam_material_name": self.beam_material_name,
            "column_material_name": self.column_material_name,
            "slab_material_name": self.slab_material_name,
            "beam_rebar_long": self.beam_rebar_long,
            "beam_rebar_tie": self.beam_rebar_tie,
            "column_rebar_long": self.column_rebar_long,
            "column_rebar_tie": self.column_rebar_tie,
            "dead_pattern": self.dead_load_pattern,
            "units": self.etabs_units_preset,
        }
        if self.etabs_present_units is not None:
            e["present_units"] = int(self.etabs_present_units)
        if self.etabs_length_scale_from_metres is not None:
            e["length_scale_from_metres"] = float(self.etabs_length_scale_from_metres)
        return e

    def apply_json_overrides(self, path: Union[str, Path]) -> "EtabsFrameDataGenerator":
        """
        Merge a JSON file over ``to_config_dict()`` and re-assign scalar fields
        that map 1:1 to this class (optional workflow: start from JSON, tweak in code).

        For full JSON-only runs, use ``run_primitive_ml_dataset.py --config ...`` instead.
        """
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        base = self.to_config_dict()

        def deep_merge(a: dict, b: dict) -> dict:
            out = dict(a)
            for k, v in b.items():
                if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                    out[k] = deep_merge(out[k], v)
                else:
                    out[k] = v
            return out

        merged = deep_merge(base, data)
        if "with_analysis_responses" in merged:
            self.with_analysis_responses = bool(merged["with_analysis_responses"])
        if "export_all_available_tables" in merged:
            self.export_all_available_tables = bool(merged["export_all_available_tables"])
        # Push merged sampling / etabs back
        s = merged.get("sampling", {})
        self.random_seed = int(s.get("seed", self.random_seed))
        self.target_accepted = int(s.get("target_accepted", self.target_accepted))
        self.max_draws = int(s.get("max_draws", self.max_draws))
        self.sampling_method = str(s.get("method", self.sampling_method)).lower()
        self.lhs_batch_size = int(s.get("lhs_batch_size", self.lhs_batch_size))

        d = merged.get("discrete", {})
        if "nx" in d:
            self.nx_values = list(d["nx"])
        if "ny" in d:
            self.ny_values = list(d["ny"])
        if "n_stories" in d:
            self.n_stories_values = list(d["n_stories"])
        if "h_story_m" in d and isinstance(d["h_story_m"], (list, tuple)):
            self.h_story_m_discrete = [float(x) for x in d["h_story_m"]]

        c = merged.get("continuous", {})

        def pull_mm(key: str, attr: str) -> None:
            if key in c and isinstance(c[key], dict):
                lo, hi = float(c[key]["min"]), float(c[key]["max"])
                setattr(self, attr, (lo, hi))

        pull_mm("Sx_m", "span_x_m")
        pull_mm("Sy_m", "span_y_m")
        pull_mm("hb_m", "beam_depth_m")
        pull_mm("bw_over_hb", "beam_width_over_depth")
        pull_mm("bcx_m", "column_side_x_m")
        pull_mm("bcy_m", "column_side_y_m")
        pull_mm("ts_m", "slab_thickness_m")
        pull_mm("col_top_area_ratio", "col_top_area_ratio")
        pull_mm("col_ext_area_ratio", "col_ext_area_ratio")

        z = merged.get("constraints", {})
        if "column_vs_beam_factor" in z:
            self.column_vs_beam_factor = float(z["column_vs_beam_factor"])
        if "column_slenderness_max" in z:
            self.column_slenderness_max = float(z["column_slenderness_max"])
        for _k, _attr in (
            ("beam_flat_max_ratio", "beam_flat_max_ratio"),
            ("column_side_ratio_max", "column_side_ratio_max"),
            ("beam_onto_column_max_ratio", "beam_onto_column_max_ratio"),
            ("beam_slab_bw_over_ts_min", "beam_slab_bw_over_ts_min"),
            ("scwb_stiffness_ratio_min", "scwb_stiffness_ratio_min"),
        ):
            if _k in z:
                setattr(self, _attr, float(z[_k]))

        def pull_band(key: str, attr: str) -> None:
            if key in z and isinstance(z[key], (list, tuple)) and len(z[key]) == 2:
                setattr(self, attr, (float(z[key][0]), float(z[key][1])))

        pull_band("hb_over_Smax", "c_hb_over_Smax")
        pull_band("ts_over_Smax", "c_ts_over_Smax")
        pull_band("plan_aspect_ratio", "c_plan_aspect_ratio")
        pull_band("internal_bay_variance", "c_internal_bay_variance")
        pull_band("elevation_aspect_ratio", "c_elevation_aspect_ratio")
        pull_band("beam_aspect_ratio", "c_beam_aspect_ratio")
        pull_band("column_aspect_ratio", "c_column_aspect_ratio")
        pull_band("column_area_index", "c_column_area_index")
        pull_band("ext_to_int_col_area_ratio", "c_ext_to_int_col_area_ratio")
        pull_band("beam_to_slab_vol_ratio", "c_beam_to_slab_vol_ratio")
        pull_band("col_vol_to_floor_vol", "c_col_vol_to_floor_vol")
        pull_band("Smax_m", "c_Smax_m")
        pull_band("h_story_m", "c_h_story_m")
        pull_band("hb_m", "c_hb_m")
        pull_band("bw_m", "c_bw_m")
        pull_band("bc_min_m", "c_bc_min_m")
        pull_band("ts_m_abs", "c_ts_m_abs")

        e = merged.get("etabs", {})
        if "material_name" in e:
            self.material_name = str(e["material_name"])
        if "beam_material_name" in e:
            self.beam_material_name = str(e["beam_material_name"])
        if "column_material_name" in e:
            self.column_material_name = str(e["column_material_name"])
        if "slab_material_name" in e:
            self.slab_material_name = str(e["slab_material_name"])
        if "beam_rebar_long" in e:
            self.beam_rebar_long = str(e["beam_rebar_long"])
        if "beam_rebar_tie" in e:
            self.beam_rebar_tie = str(e["beam_rebar_tie"])
        if "column_rebar_long" in e:
            self.column_rebar_long = str(e["column_rebar_long"])
        if "column_rebar_tie" in e:
            self.column_rebar_tie = str(e["column_rebar_tie"])
        if "dead_pattern" in e:
            self.dead_load_pattern = str(e["dead_pattern"])
        if "units" in e or "units_preset" in e:
            self.etabs_units_preset = str(e.get("units") or e.get("units_preset", "mks")).strip().lower()
        if "present_units" in e:
            self.etabs_present_units = int(e["present_units"])
        if "length_scale_from_metres" in e:
            self.etabs_length_scale_from_metres = float(e["length_scale_from_metres"])

        return self

    def generate(
        self,
        output_dir: Union[str, Path],
        *,
        no_etabs: bool = False,
        limit: int = 0,
        max_draws: int = 0,
        with_analysis_responses: Optional[bool] = None,
        super_dead_value: float = 1.0,
        live_value: float = 2.0,
        seismic_x_name: str = "QX",
        seismic_y_name: str = "QY",
        seismic_ss: float = 1.0,
        seismic_s1: float = 0.4,
        seismic_r: float = 8.0,
        seismic_site_class: int = 3,
        export_all_available_tables: Optional[bool] = None,
    ) -> int:
        cfg = self.to_config_dict()
        return run_primitive_generation(
            cfg,
            Path(output_dir),
            no_etabs=no_etabs,
            limit=limit,
            max_draws=max_draws,
            with_analysis_responses=with_analysis_responses,
            super_dead_value=super_dead_value,
            live_value=live_value,
            seismic_x_name=seismic_x_name,
            seismic_y_name=seismic_y_name,
            seismic_ss=seismic_ss,
            seismic_s1=seismic_s1,
            seismic_r=seismic_r,
            seismic_site_class=seismic_site_class,
            export_all_available_tables=export_all_available_tables,
        )

    def print_range_summary(self) -> None:
        """Console summary similar to steel connection generator startup prints."""
        print("=" * 72)
        print("ETABS FRAME DATA GENERATOR (primitive-first)")
        print("=" * 72)
        print(f"Seed: {self.random_seed}  Method: {self.sampling_method}")
        print(f"Target accepted: {self.target_accepted}  Max draws: {self.max_draws}")
        print("Discrete - nx:", self.nx_values, " ny:", self.ny_values, " stories:", self.n_stories_values)
        print(f"Continuous - span_x_m {self.span_x_m}  span_y_m {self.span_y_m}")
        print(f"  h_story_m (discrete m) {self.h_story_m_discrete}  beam_depth_m {self.beam_depth_m}")
        print(f"  beam_width/depth {self.beam_width_over_depth}  slab_thickness_m {self.slab_thickness_m}")
        print(f"  column sides (m) x {self.column_side_x_m}  y {self.column_side_y_m}")
        print(f"Constraints - hb/Smax {self.c_hb_over_Smax}  ts/Smax {self.c_ts_over_Smax}")
        print(f"  column_vs_beam_factor={self.column_vs_beam_factor}  slenderness_max={self.column_slenderness_max}")
        print(
            f"ETABS units preset: {self.etabs_units_preset}"
            + (
                f"  (override present_units={self.etabs_present_units}, "
                f"length_scale={self.etabs_length_scale_from_metres})"
                if self.etabs_present_units is not None or self.etabs_length_scale_from_metres is not None
                else ""
            )
        )
        print("=" * 72)


def main() -> int:
    p = argparse.ArgumentParser(description="ETABS frame ML data gen (class-style ranges, like steel connection script)")
    p.add_argument("--output", "-o", default="ml_etabs_frame_output", help="Output directory")
    p.add_argument("--seed", type=int, default=42, help="Random seed (LHS / discrete rotation)")
    p.add_argument("--target", type=int, default=200, help="Number of accepted models")
    p.add_argument("--max-draws", type=int, default=20000, help="Maximum sampling attempts")
    p.add_argument("--method", choices=("lhs", "random"), default="lhs", help="Continuous sampling method")
    p.add_argument("--no-etabs", action="store_true", help="Metadata only, no .edb")
    p.add_argument("--limit", type=int, default=0, help="Override --target if > 0")
    p.add_argument("--material-name", type=str, default="C40", help="Default concrete material (fallback for all sections).")
    p.add_argument("--beam-material-name", type=str, default="C40", help="Concrete material for beam sections.")
    p.add_argument("--column-material-name", type=str, default="C40", help="Concrete material for column sections.")
    p.add_argument("--slab-material-name", type=str, default="C40", help="Concrete material for slab sections.")
    p.add_argument("--beam-rebar-long", type=str, default="A615Gr60", help="Beam longitudinal rebar material.")
    p.add_argument("--beam-rebar-tie", type=str, default="A615Gr60", help="Beam tie/stirrup rebar material.")
    p.add_argument("--column-rebar-long", type=str, default="A615Gr60", help="Column longitudinal rebar material.")
    p.add_argument("--column-rebar-tie", type=str, default="A615Gr60", help="Column tie rebar material.")
    p.add_argument(
        "--with-analysis-responses",
        action="store_true",
        help="Force post-build pipeline on (default follows class / merged JSON: with_analysis_responses).",
    )
    p.add_argument(
        "--no-analysis-responses",
        action="store_true",
        help="Skip post-build pipeline (EDB only) even if class or JSON enables it.",
    )
    p.add_argument(
        "--super-dead-value",
        type=float,
        default=1.0,
        help="Super dead slab area load value used in response pipeline.",
    )
    p.add_argument(
        "--live-value",
        type=float,
        default=2.0,
        help="Live slab area load value used in response pipeline.",
    )
    p.add_argument("--seismic-x-name", type=str, default="QX", help="Auto seismic X pattern name for response export.")
    p.add_argument("--seismic-y-name", type=str, default="QY", help="Auto seismic Y pattern name for response export.")
    p.add_argument("--seismic-ss", type=float, default=1.0, help="ASCE Ss for auto seismic response export.")
    p.add_argument("--seismic-s1", type=float, default=0.4, help="ASCE S1 for auto seismic response export.")
    p.add_argument("--seismic-r", type=float, default=8.0, help="ASCE R for auto seismic response export.")
    p.add_argument("--seismic-site-class", type=int, default=3, help="ASCE site class 1..6 for response export.")
    p.add_argument(
        "--export-all-available-tables",
        action="store_true",
        help="When response export is enabled, also dump all API-available tables to CSV.",
    )
    p.add_argument(
        "--merge-json",
        type=Path,
        default=None,
        help="Optional JSON (same schema as primitive_ml_dataset.default.json) merged over class defaults",
    )
    p.add_argument(
        "--units",
        default="mks",
        choices=list_unit_preset_names(),
        help="ETABS unit preset: mks (kN,m,C), si (N,mm,C), us (kip,ft,F); sampling stays in metres",
    )
    args = p.parse_args()

    gen = EtabsFrameDataGenerator(
        random_seed=args.seed,
        target_accepted=args.target,
        max_draws=args.max_draws,
        sampling_method=args.method,
        etabs_units_preset=args.units,
    )
    gen.material_name = args.material_name
    gen.beam_material_name = args.beam_material_name
    gen.column_material_name = args.column_material_name
    gen.slab_material_name = args.slab_material_name
    gen.beam_rebar_long = args.beam_rebar_long
    gen.beam_rebar_tie = args.beam_rebar_tie
    gen.column_rebar_long = args.column_rebar_long
    gen.column_rebar_tie = args.column_rebar_tie
    if args.merge_json is not None:
        gen.apply_json_overrides(args.merge_json)
    gen.print_range_summary()

    lim = args.limit if args.limit > 0 else 0
    if args.no_analysis_responses:
        wa: Optional[bool] = False
    elif args.with_analysis_responses:
        wa = True
    else:
        wa = None
    ea: Optional[bool] = True if args.export_all_available_tables else None

    return gen.generate(
        args.output,
        no_etabs=args.no_etabs,
        limit=lim,
        max_draws=args.max_draws,
        with_analysis_responses=wa,
        super_dead_value=args.super_dead_value,
        live_value=args.live_value,
        seismic_x_name=args.seismic_x_name,
        seismic_y_name=args.seismic_y_name,
        seismic_ss=args.seismic_ss,
        seismic_s1=args.seismic_s1,
        seismic_r=args.seismic_r,
        seismic_site_class=args.seismic_site_class,
        export_all_available_tables=ea,
    )


if __name__ == "__main__":
    raise SystemExit(main())
