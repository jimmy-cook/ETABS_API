"""
Primitive-first ML dataset generator: sample physical parameters, filter with
engineering constraints, build ETABS models, and optionally run the full post-build
pipeline (slab loads, ASCE 7-16 or 1997 UBC auto seismic, mass source, analysis, CSV export).

  python scripts/run_primitive_ml_dataset.py --config configs/primitive_ml_dataset.default.json --out ./ml_models --no-etabs

  pip install -e . && etabs-primitive-gen --config configs/primitive_ml_dataset.default.json --out ./ml_models --limit 50

With ``with_analysis_responses`` true in the JSON (default under ``configs/primitive_ml_dataset.default.json``),
each saved ``.edb`` also gets ``structured_analysis_export.run_pipeline`` unless you pass
``--no-analysis-responses``.
"""

from __future__ import annotations

import argparse
import csv

import numpy as np
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .primitive_constraints import validate_building
from .primitive_resolve import PrimitiveSample, flat_record_from_primitive, resolve_from_primitives
from .primitive_sampling import load_config, sample_stream

# Reuse ETABS builder from ratio-based pipeline
from .etabs_units import list_unit_preset_names, resolve_units
from .run_parametric_dataset import build_etabs_frame_model


def _default_config_path() -> Path:
    """``configs/primitive_ml_dataset.default.json`` next to the ``src/`` directory."""
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    return repo_root / "configs" / "primitive_ml_dataset.default.json"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def _fingerprint(prim: PrimitiveSample) -> str:
    s = json.dumps(prim.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def run_primitive_generation(
    cfg: Dict[str, Any],
    out: Path,
    *,
    no_etabs: bool = False,
    limit: int = 0,
    max_draws: int = 0,
    with_analysis_responses: Optional[bool] = None,
    super_dead_value: float = 1.0,
    live_value: float = 2.0,
    seismic_x_name: str = "QX",
    seismic_y_name: str = "QY",
    seismic_design_code: str = "asce7_16",
    seismic_code_override: Optional[str] = None,
    seismic_ss: float = 1.0,
    seismic_s1: float = 0.4,
    seismic_r: float = 8.0,
    seismic_site_class: int = 3,
    ubc97: Optional[Dict[str, Any]] = None,
    export_all_available_tables: Optional[bool] = None,
    no_analysis_responses: bool = False,
    super_dead_pattern: str = "SUPERDEAD",
    live_pattern: str = "LIVE",
) -> int:
    """
    Core loop: sample from cfg → filter → optional ETABS save.

    Optional top-level JSON keys (same file as sampling/constraints):

    - ``with_analysis_responses`` (bool): after each ``.edb`` save, run
      ``structured_analysis_export.run_pipeline`` (diaphragms, SUPERDEAD/LIVE on
      slabs, auto seismic QX/QY per ``seismic_design_code``, mass source, static + modal cases,
      ``RunAnalysis``, CSV export under ``models/<id>/responses/``).
    - ``export_all_available_tables`` (bool): also dump every ETABS table API key.
    - ``analysis_inputs``: optional dict with ``super_dead_value``, ``live_value``,
      ``seismic_design_code`` (``\"asce7_16\"`` or ``\"ubc97\"``),
      ``seismic_x_name``, ``seismic_y_name``, ``seismic_ss``, ``seismic_s1``,
      ``seismic_r``, ``seismic_site_class`` (ASCE only), ``ubc97`` (nested dict for ``AutoSeismic.SetUBC97``),
      optional ``present_units``, ``csi_product``, ``apply_csi_table_output_options``,
      ``csi_table_output_options`` (merged dict for ``csi_table_output_options`` module),
      optional ``asce716_load_combinations`` (``apply`` / ``template_combos``, ``design_sets``,
      ``use_etabs_default_combos``,
      ``custom_combos`` for user-defined linear-add combos, ``replace_existing_combos``; see
      ``asce7_16_load_setup`` helpers ``asce716_load_config_from_analysis_inputs``,
      ``parse_custom_combos_from_dict``, ``apply_response_combinations``, ``apply_custom_response_combinations``).

    If ``with_analysis_responses`` is ``None``, the value comes from JSON
    ``with_analysis_responses`` (recommended so one config file defines the full workflow).
    Explicit ``True``/``False`` from callers overrides that. ``--no-analysis-responses`` on the
    CLI forces off even when the config is true.
    """
    samp = cfg["sampling"]
    target = limit if limit > 0 else int(samp["target_accepted"])
    md = max_draws if max_draws > 0 else int(samp["max_draws"])
    seed = int(samp.get("seed", 0))
    rng = np.random.default_rng(seed)

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    models_root = out / "models"
    models_root.mkdir(parents=True, exist_ok=True)
    (out / "config_resolved.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    jsonl_path = out / "runs.jsonl"
    if jsonl_path.exists():
        jsonl_path.unlink()

    constraints = cfg.get("constraints", {})

    response_present_units: Optional[str] = None
    response_csi_product: str = "etabs"
    response_apply_csi_table_output_options: bool = True
    response_csi_table_output_options: Optional[Dict[str, Any]] = None
    response_asce716_load_combinations: Optional[Dict[str, Any]] = None

    ai = cfg.get("analysis_inputs")
    if isinstance(ai, dict):
        if "super_dead_value" in ai:
            super_dead_value = float(ai["super_dead_value"])
        if "live_value" in ai:
            live_value = float(ai["live_value"])
        if "super_dead_pattern" in ai:
            super_dead_pattern = str(ai["super_dead_pattern"])
        if "live_pattern" in ai:
            live_pattern = str(ai["live_pattern"])
        if "seismic_design_code" in ai:
            seismic_design_code = str(ai["seismic_design_code"])
        if "seismic_x_name" in ai:
            seismic_x_name = str(ai["seismic_x_name"])
        if "seismic_y_name" in ai:
            seismic_y_name = str(ai["seismic_y_name"])
        if "seismic_ss" in ai:
            seismic_ss = float(ai["seismic_ss"])
        if "seismic_s1" in ai:
            seismic_s1 = float(ai["seismic_s1"])
        if "seismic_r" in ai:
            seismic_r = float(ai["seismic_r"])
        if "seismic_site_class" in ai:
            seismic_site_class = int(ai["seismic_site_class"])
        raw_ubc = ai.get("ubc97")
        if isinstance(raw_ubc, dict):
            ubc97 = dict(raw_ubc)
        if "present_units" in ai:
            pv = ai["present_units"]
            response_present_units = None if pv is None else (str(pv).strip() or None)
        if "csi_product" in ai:
            response_csi_product = str(ai["csi_product"])
        if "apply_csi_table_output_options" in ai:
            response_apply_csi_table_output_options = bool(ai["apply_csi_table_output_options"])
        raw_to = ai.get("csi_table_output_options")
        if isinstance(raw_to, dict):
            response_csi_table_output_options = dict(raw_to)
        raw_lc = ai.get("asce716_load_combinations")
        if isinstance(raw_lc, dict):
            response_asce716_load_combinations = dict(raw_lc)

    if seismic_code_override:
        seismic_design_code = str(seismic_code_override)

    if no_etabs or no_analysis_responses:
        with_analysis_responses = False
        export_all_available_tables = False
    else:
        wa_cfg = bool(cfg.get("with_analysis_responses", False))
        ea_cfg = bool(cfg.get("export_all_available_tables", False))
        if with_analysis_responses is not None:
            with_analysis_responses = bool(with_analysis_responses)
        else:
            with_analysis_responses = wa_cfg
        if export_all_available_tables is not None:
            export_all_available_tables = bool(export_all_available_tables)
        else:
            export_all_available_tables = ea_cfg

    if with_analysis_responses and not no_etabs:
        print(
            "Post-build pipeline enabled: diaphragms, SUPERDEAD/LIVE slab loads, "
            "auto seismic (ASCE 7-16 or UBC 97 per config), mass source, analysis, CSV export under each model's responses/."
        )

    etabs_opts = cfg.get("etabs", {})
    mat_name = str(etabs_opts.get("material_name", "C40"))
    beam_mat_name = str(etabs_opts.get("beam_material_name", mat_name))
    column_mat_name = str(etabs_opts.get("column_material_name", mat_name))
    slab_mat_name = str(etabs_opts.get("slab_material_name", mat_name))
    beam_rebar_long = str(etabs_opts.get("beam_rebar_long", "A615Gr60"))
    beam_rebar_tie = str(etabs_opts.get("beam_rebar_tie", beam_rebar_long))
    column_rebar_long = str(etabs_opts.get("column_rebar_long", beam_rebar_long))
    column_rebar_tie = str(etabs_opts.get("column_rebar_tie", column_rebar_long))
    dead_pat = str(etabs_opts.get("dead_pattern", "DEAD"))
    mat_strength_sys = str(etabs_opts.get("material_strength_system", "auto"))
    concrete_fc_mpa = float(etabs_opts.get("concrete_fc_mpa", 40))
    concrete_fc_psi = float(etabs_opts.get("concrete_fc_psi", 4000))
    rebar_fy_ksi = float(etabs_opts.get("rebar_fy_ksi", 60))
    rebar_fu_ksi = float(etabs_opts.get("rebar_fu_ksi", 90))
    beam_cover_in = float(etabs_opts.get("beam_cover_in", 1.5))
    column_cover_in = float(etabs_opts.get("column_cover_in", 1.5))
    column_tie_spacing_in = float(etabs_opts.get("column_tie_spacing_in", 6))
    column_main_bar_size = str(etabs_opts.get("column_main_bar_size", "#6"))
    column_tie_bar_size = str(etabs_opts.get("column_tie_bar_size", "#4"))
    preset = etabs_opts.get("units") or etabs_opts.get("units_preset")
    code_ov = etabs_opts.get("present_units")
    if code_ov is not None:
        code_ov = int(code_ov)
    scale_ov = etabs_opts.get("length_scale_from_metres")
    if scale_ov is not None:
        scale_ov = float(scale_ov)
    present_units, length_scale = resolve_units(
        str(preset) if preset is not None else None,
        present_units_code=code_ov,
        length_scale_from_metres=scale_ov,
    )
    print(
        f"ETABS InitializeNewModel units={present_units}, "
        f"length_scale_from_metres={length_scale} (geometry scaled for ETABS length unit)"
    )

    accepted_rows: List[dict] = []
    seen: Set[str] = set()
    reject_counter: Counter[str] = Counter()
    draws = 0

    stream = sample_stream(cfg, rng=rng)

    while len(accepted_rows) < target and draws < md:
        prim, draw_idx = next(stream)
        draws += 1
        rb = resolve_from_primitives(prim)
        ok, reasons = validate_building(rb, constraints)
        if not ok:
            reject_counter[reasons[0].split("=")[0] if reasons else "unknown"] += 1
            continue

        fp = _fingerprint(prim)
        if fp in seen:
            reject_counter["duplicate"] += 1
            continue
        seen.add(fp)

        row = flat_record_from_primitive(rb, prim)
        row["draw_index"] = draw_idx
        row["accepted_index"] = len(accepted_rows)
        row["fingerprint"] = fp
        model_id = f"model_{len(accepted_rows):05d}_{fp[:8]}"
        row["model_id"] = model_id

        row["etabs_present_units"] = present_units
        row["etabs_length_scale_from_metres"] = length_scale

        if no_etabs:
            row["edb_path"] = ""
            row["status"] = "accepted_geometry_only"
        else:
            try:
                from .etabs_api import EtabsConnection
            except ImportError:
                print("etabs_ml.etabs_api could not be imported (install with: pip install -e .).", file=sys.stderr)
                return 1
            conn = EtabsConnection(attach_to_active=False)
            conn.connect()
            if not conn.sap_model:
                print("Could not connect to ETABS.", file=sys.stderr)
                return 1
            edb: Optional[Path] = None
            model_dir: Optional[Path] = None
            try:
                model_dir = (models_root / model_id).resolve()
                model_dir.mkdir(parents=True, exist_ok=True)
                stats = build_etabs_frame_model(
                    conn.sap_model,
                    rb,
                    mat_name=mat_name,
                    beam_mat_name=beam_mat_name,
                    column_mat_name=column_mat_name,
                    slab_mat_name=slab_mat_name,
                    beam_rebar_long=beam_rebar_long,
                    beam_rebar_tie=beam_rebar_tie,
                    column_rebar_long=column_rebar_long,
                    column_rebar_tie=column_rebar_tie,
                    dead_pattern=dead_pat,
                    present_units=present_units,
                    length_scale_from_metres=length_scale,
                    material_strength_system=mat_strength_sys,
                    concrete_fc_mpa=concrete_fc_mpa,
                    concrete_fc_psi=concrete_fc_psi,
                    rebar_fy_ksi=rebar_fy_ksi,
                    rebar_fu_ksi=rebar_fu_ksi,
                    beam_cover_in=beam_cover_in,
                    column_cover_in=column_cover_in,
                    column_tie_spacing_in=column_tie_spacing_in,
                    column_main_bar_size=column_main_bar_size,
                    column_tie_bar_size=column_tie_bar_size,
                )
                edb = (model_dir / "model.edb").resolve()
                conn.sap_model.File.Save(str(edb))
                if not edb.is_file():
                    raise RuntimeError(f"File.Save did not create {edb}")
                row["edb_path"] = str(edb)
                row["model_dir"] = str(model_dir)
                row["status"] = "ok"
                for k, v in stats.items():
                    row[f"mesh_{k}"] = v

                model_meta = {
                    "model_id": model_id,
                    "accepted_index": row["accepted_index"],
                    "fingerprint": fp,
                    "edb_path": str(edb),
                    "draw_index": draw_idx,
                    "utc_iso": datetime.now(timezone.utc).isoformat(),
                }
                (model_dir / "model_meta.json").write_text(
                    json.dumps(model_meta, indent=2),
                    encoding="utf-8",
                )

                if with_analysis_responses:
                    try:
                        from .structured_analysis_export import run_pipeline
                    except ImportError:
                        raise RuntimeError(
                            "structured_analysis_export.py is required for --with-analysis-responses"
                        )
                    try:
                        responses_root = model_dir / "responses"
                        rep = run_pipeline(
                            conn.sap_model,
                            responses_root,
                            model_path=edb,
                            assign_diaphragms=True,
                            assign_slab_loads=True,
                            super_dead_pattern=str(super_dead_pattern),
                            super_dead_value=float(super_dead_value),
                            live_pattern=str(live_pattern),
                            live_value=float(live_value),
                            seismic_x_name=str(seismic_x_name),
                            seismic_y_name=str(seismic_y_name),
                            seismic_design_code=str(seismic_design_code),
                            seismic_ss=float(seismic_ss),
                            seismic_s1=float(seismic_s1),
                            seismic_r=float(seismic_r),
                            seismic_site_class=int(seismic_site_class),
                            ubc97=dict(ubc97) if isinstance(ubc97, dict) else None,
                            mass_source=True,
                            ensure_modal=True,
                            run_analysis=True,
                            export_all_available=bool(export_all_available_tables),
                            present_units=response_present_units,
                            csi_product=str(response_csi_product),
                            apply_csi_table_output_options=bool(
                                response_apply_csi_table_output_options
                            ),
                            csi_table_output_options=response_csi_table_output_options,
                            asce716_load_combinations=response_asce716_load_combinations,
                            dead_load_pattern=str(dead_pat),
                        )
                        row["response_manifest_path"] = str(rep.get("manifest_path", ""))
                        row["response_run_directory"] = str(rep.get("run_directory", ""))
                    except Exception as pipe_exc:  # noqa: BLE001
                        # Keep EDB; analysis/export is optional to diagnose separately.
                        row["status"] = f"ok_edb; response_pipeline_error: {pipe_exc}"
                        row["response_pipeline_error"] = str(pipe_exc)
            except Exception as e:
                row["edb_path"] = str(edb) if edb is not None and edb.is_file() else ""
                if model_dir is not None:
                    row["model_dir"] = str(model_dir)
                else:
                    row["model_dir"] = ""
                row["status"] = f"error: {e}"
                if not row["edb_path"]:
                    row["model_dir"] = ""
            finally:
                try:
                    conn.close(save_model=False)
                except Exception as close_exc:  # noqa: BLE001
                    # ETABS can terminate its COM server before ApplicationExit returns.
                    # Keep batch generation running even if close handshake fails.
                    row["close_warning"] = f"{type(close_exc).__name__}: {close_exc}"

        _append_jsonl(jsonl_path, row)
        accepted_rows.append(row)

    _write_csv(out / "summary.csv", accepted_rows)
    batch_manifest = {
        "utc_iso": datetime.now(timezone.utc).isoformat(),
        "output_root": str(out.resolve()),
        "models_root": str(models_root.resolve()),
        "target_accepted": target,
        "accepted": len(accepted_rows),
        "total_draws": draws,
        "with_analysis_responses": bool(with_analysis_responses),
        "super_dead_value": float(super_dead_value),
        "live_value": float(live_value),
        "seismic_x_name": str(seismic_x_name),
        "seismic_y_name": str(seismic_y_name),
        "seismic_design_code": str(seismic_design_code),
        "seismic_ss": float(seismic_ss),
        "seismic_s1": float(seismic_s1),
        "seismic_r": float(seismic_r),
        "seismic_site_class": int(seismic_site_class),
        "ubc97": dict(ubc97) if isinstance(ubc97, dict) else None,
        "export_all_available_tables": bool(export_all_available_tables),
        "super_dead_pattern": str(super_dead_pattern),
        "live_pattern": str(live_pattern),
        "model_ids": [r.get("model_id", "") for r in accepted_rows],
    }
    (out / "batch_manifest.json").write_text(
        json.dumps(batch_manifest, indent=2),
        encoding="utf-8",
    )

    print(f"Target accepted: {target}")
    print(f"Accepted: {len(accepted_rows)}  Total draws: {draws}  Max draws: {md}")
    if reject_counter:
        print("Top reject reasons (first token):", reject_counter.most_common(12))
    print(f"Wrote {jsonl_path}, summary.csv, batch_manifest.json under {out.resolve()}")
    if len(accepted_rows) < target:
        print("Warning: fewer than target accepted — relax constraints or increase max_draws.", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Primitive-first ETABS ML model batch (optional analysis responses)")
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="JSON (or YAML with PyYAML) config path (default: configs/primitive_ml_dataset.default.json)",
    )
    ap.add_argument("--out", type=Path, default=Path("ml_primitive_output"), help="Output directory")
    ap.add_argument("--no-etabs", action="store_true", help="Validate + write metadata only")
    ap.add_argument("--limit", type=int, default=0, help="Max accepted models (0 = use config target_accepted)")
    ap.add_argument("--max-draws", type=int, default=0, help="Override config max_draws (0 = use config)")
    ap.add_argument(
        "--with-analysis-responses",
        action="store_true",
        help="After each model is saved, run analysis export and store responses per model.",
    )
    ap.add_argument(
        "--no-analysis-responses",
        action="store_true",
        help="Skip post-build pipeline even if config sets with_analysis_responses (EDB only).",
    )
    ap.add_argument(
        "--super-dead-value",
        type=float,
        default=1.0,
        help="Super dead slab area load value passed to response pipeline.",
    )
    ap.add_argument(
        "--live-value",
        type=float,
        default=2.0,
        help="Live slab area load value passed to response pipeline.",
    )
    ap.add_argument(
        "--seismic-design-code",
        type=str,
        default=None,
        metavar="CODE",
        help='Override JSON analysis_inputs.seismic_design_code: "asce7_16" or "ubc97".',
    )
    ap.add_argument("--seismic-x-name", type=str, default="QX", help="Auto seismic X pattern name for response export.")
    ap.add_argument("--seismic-y-name", type=str, default="QY", help="Auto seismic Y pattern name for response export.")
    ap.add_argument("--seismic-ss", type=float, default=1.0, help="ASCE Ss for auto seismic response export.")
    ap.add_argument("--seismic-s1", type=float, default=0.4, help="ASCE S1 for auto seismic response export.")
    ap.add_argument("--seismic-r", type=float, default=8.0, help="ASCE R for auto seismic response export.")
    ap.add_argument("--seismic-site-class", type=int, default=3, help="ASCE site class 1..6 for response export.")
    ap.add_argument(
        "--export-all-available-tables",
        action="store_true",
        help="When response export is enabled, also dump all API-available tables to CSV.",
    )
    ap.add_argument(
        "--units",
        default=None,
        choices=list_unit_preset_names(),
        metavar="PRESET",
        help="Override config etabs.units (mks, si, us, …; see etabs_units.UNIT_PRESETS)",
    )
    args = ap.parse_args(argv)
    if args.seismic_design_code is not None:
        sc = str(args.seismic_design_code).strip().lower().replace("-", "_")
        if sc not in ("asce7_16", "ubc97"):
            ap.error('--seismic-design-code must be "asce7_16" or "ubc97"')

    config_path = args.config if args.config is not None else _default_config_path()
    cfg = load_config(config_path)
    if args.units is not None:
        cfg.setdefault("etabs", {})["units"] = args.units

    if args.no_analysis_responses:
        wa_arg: Optional[bool] = False
    elif args.with_analysis_responses:
        wa_arg = True
    else:
        wa_arg = None
    ea_arg: Optional[bool] = True if args.export_all_available_tables else None

    return run_primitive_generation(
        cfg,
        args.out,
        no_etabs=args.no_etabs,
        limit=args.limit,
        max_draws=args.max_draws,
        with_analysis_responses=wa_arg,
        super_dead_value=args.super_dead_value,
        live_value=args.live_value,
        seismic_x_name=args.seismic_x_name,
        seismic_y_name=args.seismic_y_name,
        seismic_code_override=args.seismic_design_code,
        seismic_ss=args.seismic_ss,
        seismic_s1=args.seismic_s1,
        seismic_r=args.seismic_r,
        seismic_site_class=args.seismic_site_class,
        export_all_available_tables=ea_arg,
        no_analysis_responses=bool(args.no_analysis_responses),
    )


if __name__ == "__main__":
    raise SystemExit(main())
