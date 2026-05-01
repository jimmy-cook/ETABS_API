"""
Single control file for ETABS frame data generation.

Edit only the USER_SETTINGS block below, then run:

    python main.py   # or: etabs-primitive-gen --config configs/... after pip install -e .

When ``with_analysis_responses`` is True (default), each saved model also runs the
full post-build pipeline (slab loads, ASCE 7-16 or 1997 UBC auto seismic per ``analysis_inputs``,
mass source, analysis, CSV export) via ``run_primitive_ml_dataset`` / ``structured_analysis_export``.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict

_REPO = Path(__file__).resolve().parent
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etabs_ml.run_primitive_ml_dataset import run_primitive_generation


# =============================================================================
# EDIT HERE ONLY
#
# Quick map of USER_SETTINGS (which block controls what):
#   output_folder          -> where all run outputs go (models/, summary.csv, …)
#   no_etabs               -> if True: only sample + validate geometry (no .edb)
#   with_analysis_responses-> if True: full post-build (slab SUPERDEAD/LIVE, auto seismic
#                             per seismic_design_code, mass source, RunAnalysis, CSV under responses/)
#   export_all_available_tables -> extra: dump every ETABS table API exposes
#   limit / max_draws      -> 0 = use config["sampling"] target_accepted / max_draws
#   analysis_inputs        -> loads + seismic (ASCE 7-16 or UBC 97) for response export only
#   config.sampling        -> how many models, random seed, LHS vs random
#   config.discrete        -> allowed integers (bays per direction, story count)
#   config.continuous      -> min/max for each random-continuous parameter (metres)
#   config.constraints     -> accept/reject bands on derived geometry (see below)
#   config.etabs           -> units (mks|si|us) + material names + concrete_fc_mpa (MKS/SI) /
#                             concrete_fc_psi (US) + rebar/cover options + dead pattern
# =============================================================================
USER_SETTINGS: Dict[str, Any] = {
    # All generated files land here: models/<model_id>/, runs.jsonl, summary.csv,
    # batch_manifest.json, config_resolved.json.
    "output_folder": "ml_primitive_output",

    # True  = no ETABS COM call; checks sampling + constraints only (fast debug).
    # False = build and save a real .edb per accepted sample.
    "no_etabs": False,

    # True = after each model file is saved, run structured_analysis_export (analysis + tables).
    "with_analysis_responses": True,

    # True = also export every table GetAllTables can return (large; for full dumps).
    "export_all_available_tables": True,

    # Cap how many models to accept / how many sampling attempts before stopping.
    # 0 means "use config['sampling']" for that value.
    "limit": 0,  # max accepted models (0 → target_accepted)
    "max_draws": 0,  # max sampling attempts including rejects (0 → max_draws in config)

    # --- Response pipeline only (ignored if with_analysis_responses is False) ---
    # Units for super_dead_value / live_value must match ETABS force/area units for
    # the chosen etabs.units (e.g. pressure for area loads on slabs).
    #
    # CSI reporting (optional; same keys as primitive_ml_dataset*.json analysis_inputs):
    #   present_units — string id for SapModel.SetPresentUnits, e.g. kN_m_C, kip_ft_F, or null to skip
    #   csi_product — "etabs" or "sap2000" (allowed present_units set)
    #   apply_csi_table_output_options — false to skip Results.Setup / DatabaseTables option calls
    #   csi_table_output_options — object; only listed keys override defaults in csi_table_output_options.py
    #     (integers: multistep_static, nonlinear_static, modal_history, direct_history, load_combo_multiple,
    #      mode_shape_*, buckling_*, base_react_*; 1=envelopes for static-style keys per CSI samples)
    #   asce716_load_combinations — optional dict: template packs ("apply" / "template_combos", "design_sets"),
    #     optional "use_etabs_default_combos": True,
    #     optional "custom_combos" { "NAME": [["PAT", sf], ...] }, "replace_existing_combos"; see
    #     asce7_16_load_setup (asce716_load_config_from_analysis_inputs, parse_custom_combos_from_dict, …)
    "analysis_inputs": {
        "super_dead_value": 1.0,  # slab super-dead uniform load magnitude
        "live_value": 2.0,  # slab live uniform load magnitude
        # "asce7_16" (default) uses Ss/S1/R/site_class below; "ubc97" uses optional "ubc97" dict for
        # LoadPatterns.AutoSeismic.SetUBC97 (PeriodFlag, Ct, T, Z, …, TopStory/BotStory names).
        "seismic_design_code": "asce7_16",
        "seismic_x_name": "QX",  # X lateral auto-seismic load pattern name
        "seismic_y_name": "QY",  # Y lateral auto-seismic load pattern name
        "seismic_ss": 1.0,  # ASCE 7-16 Ss (short-period spectral acceleration)
        "seismic_s1": 0.4,  # ASCE 7-16 S1 (1s spectral acceleration)
        "seismic_r": 8.0,  # ASCE 7-16 R (response modification)
        "seismic_site_class": 3,  # ASCE site class 1–6
        # Example for UBC 97: set "seismic_design_code": "ubc97" and uncomment / edit:
        # "ubc97": {"period_flag": 0, "ct": 0.0731, "z": 0.4, "soil_profile": 4, "r": 8.5, "top_story": "Story3", "bottom_story": "Story1"},
        "csi_product": "etabs",
        "apply_csi_table_output_options": True,
        "present_units": None,
        "csi_table_output_options": {
            "multistep_static": 1,
            "load_combo_multiple": 1,
        },
        "asce716_load_combinations": {
            "apply": True,
            "design_sets": ["concrete_frame", "steel_frame", "slab_gravity"],
            "include_wind": False,
        },
    },

    "config": {
        "description": "Single-file ETABS frame ML data generation settings.",

        # --- Sampling process (see primitive_sampling.py) ---
        "sampling": {
            "method": "lhs",  # "lhs" = Latin hypercube in [0,1] per batch; "random" = i.i.d. uniform
            "target_accepted": 200,  # number of unique accepted models to generate
            "seed": 42,  # fixes numpy RNG for reproducible sequences
            "max_draws": 20000,  # hard stop on total tries (rejects + duplicates count)
            "lhs_batch_size": 2048,  # for method=lhs, redraw LHS matrix every this many tries
        },

        # --- Discrete grid (only these integers are used; cycled with continuous draws) ---
        "discrete": {
            "nx": [2, 3, 4, 5],  # number of bays along global X
            "ny": [2, 3, 4, 5],  # number of bays along global Y
            "n_stories": [2, 3, 4, 5, 6, 7, 8, 9, 10],  # above-ground levels
            # Floor-to-floor: exactly 10, 11, 12 ft as metres (1 ft = 0.3048 m)
            "h_story_m": [3.048, 3.3528, 3.6576],
        },

        # --- What gets random-sampled: each {min, max} is one dimension (metres, except ratios) ---
        # Sx_m, Sy_m: single-bay span length; plan size = nx*Sx by ny*Sy.
        # h_story_m: if listed under discrete, only those values are used (omit from continuous).
        # hb_m, bw_over_hb: beam depth; beam width = hb * bw_over_hb.
        # bcx_m, bcy_m: column rectangle sides (reordered in solver so min side is bcx if needed).
        # col_top_area_ratio: top-story col area / interior base col area (sides scale by sqrt(ratio)).
        # col_ext_area_ratio: exterior col area / interior base col area (sides scale by sqrt(ratio)).
        "continuous": {
            "Sx_m": {"min": 4.0, "max": 9.0},
            "Sy_m": {"min": 4.0, "max": 9.0},
            "hb_m": {"min": 0.42, "max": 0.95},
            "bw_over_hb": {"min": 0.4, "max": 0.8},
            "bcx_m": {"min": 0.35, "max": 0.65},
            "bcy_m": {"min": 0.4, "max": 0.85},
            "ts_m": {"min": 0.18, "max": 0.32},
            "col_top_area_ratio": {"min": 0.35, "max": 1.0},
            "col_ext_area_ratio": {"min": 0.55, "max": 1.0},
        },

        # --- Post-build acceptance checks (see primitive_constraints.validate_building) ---
        # Each [lo,hi] is inclusive. Ratios/indices come from rb.achieved; lengths in metres.
        "constraints": {
            "hb_over_Smax": [0.06, 0.14],  # beam depth / max bay span
            "ts_over_Smax": [0.03, 0.08],  # slab thickness / max bay span
            "plan_aspect_ratio": [0.25, 1.0],  # min/max plan aspect Ly/Lx
            "internal_bay_variance": [0.45, 1.0],  # Sx/Sy homogeneity
            "elevation_aspect_ratio": [0.15, 1.0],  # height vs plan scale
            "beam_aspect_ratio": [0.35, 0.85],  # achieved bw/hb (width / depth)
            "column_aspect_ratio": [0.45, 1.0],  # achieved min(bcx,bcy)/max(...)
            "column_area_index": [0.25, 1.0],  # Acol_top / Acol_base (top vs interior base)
            "ext_to_int_col_area_ratio": [0.45, 1.0],  # Acol_ext / Acol_int (exterior vs interior)
            "beam_to_slab_vol_ratio": [0.12, 1.15],  # Vbeam / Vslab (all stories)
            "col_vol_to_floor_vol": [0.06, 0.95],  # Vcol / (Vbeam + Vslab)
            "Smax_m": [3.5, 11.0],  # max(Sx, Sy) allowed range
            "h_story_m": [3.048, 3.6576],  # resolved story height (m), matches 10–12 ft band
            "hb_m": [0.28, 1.05],  # resolved beam depth
            "bw_m": [0.18, 0.75],  # resolved beam width
            "bc_min_m": [0.3, 0.9],  # min(bcx, bcy) after resolution
            "ts_m_abs": [0.12, 0.38],  # slab thickness
            "column_vs_beam_factor": 0.62,  # require min col dim >= factor * hb (0 = off)
            "column_slenderness_max": 28.0,  # max story_height / min column side (0 = off)
            # Explicit rules (see primitive_constraints docstring; 0 = off):
            "beam_flat_max_ratio": 1.0,  # bw/hb <= this (no flat beam)
            "column_side_ratio_max": 3.0,  # max(bcx,bcy)/min < this
            "beam_onto_column_max_ratio": 1.0,  # bw <= ratio * min(bcx,bcy)
            "beam_slab_bw_over_ts_min": 2.5,  # bw >= k * ts
            "scwb_stiffness_ratio_min": 0.5,  # Icol/Lcol >= k * Ibeam/Smax (crude filter)
        },

        # --- Passed into ETABS builder (run_parametric_dataset.build_etabs_frame_model) ---
        "etabs": {
            "units": "mks",  # mks|si|us|… — see etabs_units.UNIT_PRESETS
            "material_name": "C40",  # fallback if a per-member name is missing
            "beam_material_name": "C40",  # concrete for beam property
            "column_material_name": "C40",  # concrete for column property
            "slab_material_name": "C40",  # concrete for slab area property
            "beam_rebar_long": "A615Gr60",  # beam longitudinal rebar material name
            "beam_rebar_tie": "A615Gr60",  # beam stirrup / tie rebar
            "column_rebar_long": "A615Gr60",  # column longitudinal rebar
            "column_rebar_tie": "A615Gr60",  # column tie/stirrup rebar
            "dead_pattern": "DEAD",  # load pattern name for default dead
            # FPS / kip-ft (units=us): auto turns on US strengths + inch→ft cover/spacing.
            "material_strength_system": "auto",
            "concrete_fc_mpa": 40,
            "concrete_fc_psi": 4000,
            "rebar_fy_ksi": 60,
            "rebar_fu_ksi": 90,
            "beam_cover_in": 1.5,
            "column_cover_in": 1.5,
            "column_tie_spacing_in": 6,
            "column_main_bar_size": "#6",
            "column_tie_bar_size": "#4",
        },
    },
}
# =============================================================================
# DO NOT EDIT BELOW UNLESS YOU WANT TO CHANGE THE PROGRAM LOGIC
# =============================================================================


def main() -> int:
    analysis_inputs = USER_SETTINGS["analysis_inputs"]
    # Single cfg dict for sampling + ETABS + full workflow (matches primitive_ml_dataset.default.json).
    cfg = copy.deepcopy(USER_SETTINGS["config"])
    cfg["with_analysis_responses"] = bool(USER_SETTINGS["with_analysis_responses"])
    cfg["export_all_available_tables"] = bool(USER_SETTINGS["export_all_available_tables"])
    cfg["analysis_inputs"] = copy.deepcopy(analysis_inputs)

    return run_primitive_generation(
        cfg,
        Path(USER_SETTINGS["output_folder"]),
        no_etabs=bool(USER_SETTINGS["no_etabs"]),
        limit=int(USER_SETTINGS["limit"]),
        max_draws=int(USER_SETTINGS["max_draws"]),
        with_analysis_responses=None,
        super_dead_value=float(analysis_inputs["super_dead_value"]),
        live_value=float(analysis_inputs["live_value"]),
        seismic_x_name=str(analysis_inputs["seismic_x_name"]),
        seismic_y_name=str(analysis_inputs["seismic_y_name"]),
        seismic_ss=float(analysis_inputs["seismic_ss"]),
        seismic_s1=float(analysis_inputs["seismic_s1"]),
        seismic_r=float(analysis_inputs["seismic_r"]),
        seismic_site_class=int(analysis_inputs["seismic_site_class"]),
        export_all_available_tables=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
