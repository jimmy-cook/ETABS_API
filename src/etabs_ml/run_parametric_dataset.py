"""
Batch-generate ETABS multi-story frame models from parametric_definitions grids.

Requires: Windows, ETABS installed, comtypes, numpy.

Example (first 20 models, save under ./parametric_output):

  python run_parametric_dataset.py --out ./parametric_output --limit 20

Full Cartesian product is large (~ product of all variation counts). Use --limit
or --stride during development.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .etabs_api import _csi_ret0
from .etabs_units import scale_resolved_building
from .parametric_building_dimensions import ResolvedBuilding, building_to_flat_dict, resolve_building
from .parametric_definitions import PARAM_AXES, iter_param_dicts, total_combination_count, write_parameter_manifest


def _tower_activate_current(sap_model) -> None:
    """Ensure ``Story.SetStories`` targets the active tower (multi-tower models)."""
    try:
        gt = sap_model.Tower.GetActiveTower()
        if isinstance(gt, (list, tuple)) and len(gt) >= 1:
            tn = gt[0]
            if tn is not None and str(tn).strip():
                sap_model.Tower.SetActiveTower(str(tn))
    except Exception:
        pass


def _get_story_names_and_elevations(sap_model) -> Tuple[Optional[List[str]], Optional[List[float]]]:
    """Parse ``Story.GetStories`` return layout (COM may use ret-first tuple)."""
    try:
        gs = sap_model.Story.GetStories()
        if not isinstance(gs, (list, tuple)) or len(gs) < 3:
            return None, None
        names = gs[1]
        elevs = gs[2]
        if names is None or elevs is None:
            return None, None
        return list(names), [float(x) for x in elevs]
    except Exception:
        return None, None


def _stories_match_requested(
    got_names: List[str],
    got_elevs: List[float],
    want_names: List[str],
    want_z: List[float],
    *,
    tol: float = 1e-3,
) -> bool:
    if len(got_names) != len(want_names) or len(got_elevs) != len(want_z):
        return False
    if [str(x) for x in got_names] != [str(x) for x in want_names]:
        return False
    for a, b in zip(got_elevs, want_z):
        if abs(a - b) > tol:
            return False
    return True


def _sync_etabs_stories_from_floor_elevations(
    sap_model,
    floor_zs: Sequence[float],
) -> Dict[str, Any]:
    """
    Replace the default ETABS story list with ``Story1`` … ``Story{N}`` matching the
    joint grid ``floor_zs`` (``N = len(floor_zs) - 1``). Elevations follow the CSI API:
    length ``N + 1`` with the first entry the base datum (``floor_zs[0]``), then all
    floor ``Z`` values through the roof joint.

    Must run **before** adding joints so ``PointObj`` / ``AreaObj`` story labels match
    the real floor count (fixes modal / diaphragm / story-output tables).
    """
    _tower_activate_current(sap_model)

    z = [float(x) for x in floor_zs]
    n = len(z)
    if n < 2:
        return {"ok": False, "reason": "need at least two floor elevations (base + one level)"}
    for i in range(1, n):
        if z[i] <= z[i - 1]:
            return {"ok": False, "reason": "floor_zs must be strictly increasing"}

    # CSI ETABS 2016+ ``SetStories`` contract (see API docs):
    # - ``StoryNames`` length = number of stories (no separate "Base" name; base is the first elevation).
    # - ``StoryElevations`` length = number of stories + 1; first value is the base datum, then floor Zs.
    # - ``StoryHeights`` / master / similar / splice arrays length = number of stories.
    n_seg = n - 1
    names = [f"Story{i}" for i in range(1, n_seg + 1)]
    elevs = list(z)
    heights = [max(z[i + 1] - z[i], 1e-12) for i in range(n_seg)]
    master = [False] * n_seg
    if n_seg >= 1:
        master[-1] = True
    similar = ["None"] + [""] * (n_seg - 1) if n_seg else []
    splice_a = [False] * n_seg
    splice_h = [0.0] * n_seg
    last_exc: Optional[str] = None
    try:
        ret = sap_model.Story.SetStories(names, elevs, heights, master, similar, splice_a, splice_h)
        if _csi_ret0(ret) == 0:
            return {
                "ok": True,
                "method": "SetStories",
                "story_names": names,
                "elevations": elevs,
                "heights": heights,
                "csi_ret": ret,
            }
        gn, ge = _get_story_names_and_elevations(sap_model)
        if gn is not None and ge is not None and _stories_match_requested(gn, ge, names, elevs):
            return {
                "ok": True,
                "method": "SetStories",
                "story_names": names,
                "elevations": elevs,
                "heights": heights,
                "csi_ret": ret,
                "note": "SetStories returned non-zero but GetStories matches requested layout",
            }
        last_exc = f"SetStories returned non-zero: {ret!r}"
    except Exception as exc:
        last_exc = str(exc)

    fn2 = getattr(sap_model.Story, "SetStories_2", None)
    if callable(fn2):
        for tower in (0, 1):
            try:
                ret = fn2(tower, names, elevs, heights, master, similar, splice_a, splice_h)
                if _csi_ret0(ret) == 0:
                    return {
                        "ok": True,
                        "method": f"SetStories_2({tower})",
                        "story_names": names,
                        "elevations": elevs,
                        "heights": heights,
                        "csi_ret": ret,
                    }
                gn, ge = _get_story_names_and_elevations(sap_model)
                if gn is not None and ge is not None and _stories_match_requested(gn, ge, names, elevs):
                    return {
                        "ok": True,
                        "method": f"SetStories_2({tower})",
                        "story_names": names,
                        "elevations": elevs,
                        "heights": heights,
                        "csi_ret": ret,
                        "note": "SetStories_2 returned non-zero but GetStories matches requested layout",
                    }
            except TypeError:
                continue
            except Exception as exc:
                last_exc = str(exc)

    return {
        "ok": False,
        "error": last_exc or "Story.SetStories failed",
        "story_names": names,
        "elevations": elevs,
    }


def _joint_index(ix: int, iy: int, nxp: int) -> int:
    return iy * nxp + ix


def _is_perimeter(ix: int, iy: int, nx: int, ny: int) -> bool:
    return ix == 0 or iy == 0 or ix == nx or iy == ny


def build_etabs_frame_model(
    sap_model,
    rb: ResolvedBuilding,
    *,
    mat_name: str = "C40",
    beam_mat_name: Optional[str] = None,
    column_mat_name: Optional[str] = None,
    slab_mat_name: Optional[str] = None,
    beam_rebar_long: str = "A615Gr60",
    beam_rebar_tie: str = "A615Gr60",
    column_rebar_long: str = "A615Gr60",
    column_rebar_tie: str = "A615Gr60",
    dead_pattern: str = "DEAD",
    present_units: int = 6,
    length_scale_from_metres: float = 1.0,
    add_default_dead_pattern: bool = True,
    # --- Material strength: US (psi / ksi / inch cover) vs metric (MPa / mm-style covers) ---
    material_strength_system: str = "auto",
    concrete_fc_mpa: float = 40.0,
    concrete_fc_psi: float = 4000.0,
    rebar_fy_ksi: float = 60.0,
    rebar_fu_ksi: float = 90.0,
    beam_cover_in: float = 1.5,
    column_cover_in: float = 1.5,
    column_tie_spacing_in: float = 6.0,
    column_main_bar_size: str = "#6",
    column_tie_bar_size: str = "#4",
) -> Dict[str, int]:
    """Populate SapModel with a regular orthogonal frame and floor shell slabs.

    ``rb`` is in **metres**; when ``length_scale_from_metres`` is not 1, lengths are
    scaled before sending to ETABS (e.g. 1000 for N–mm–C, 1/0.3048 for kip–ft).
    ``present_units`` is the CSI ``InitializeNewModel`` code (see ``etabs_units``).

    **MKS (6)** / **SI (9)**: ``auto`` uses ``concrete_fc_mpa`` and metric rebar (MPa-scale ``SetORebar``).

    **US / FPS (4)**: ``auto`` uses ``concrete_fc_psi``, ksi rebar, and inch-based cover/spacing (→ ft).
    """
    from .etabs_api import EtabsGeometry, EtabsLoading, EtabsModel

    rb = scale_resolved_building(rb, float(length_scale_from_metres))

    model = EtabsModel(sap_model)
    model.initialize_new_model(units=int(present_units))
    model.create_blank_model()
    # Define stories before materials / diaphragms / joints — late SetStories often returns
    # non-zero on some builds once the blank model has accumulated objects.
    nx, ny = rb.nx, rb.ny
    nxp, nyp = nx + 1, ny + 1
    nz = rb.n_stories + 1
    zs = [i * rb.story_height for i in range(nz)]
    story_sync = _sync_etabs_stories_from_floor_elevations(sap_model, zs)

    beam_mat = str(beam_mat_name or mat_name)
    col_mat = str(column_mat_name or mat_name)
    slab_mat = str(slab_mat_name or mat_name)

    sys_raw = str(material_strength_system).strip().lower()
    if sys_raw in ("auto", ""):
        use_us_strength = int(present_units) == 4
    else:
        use_us_strength = sys_raw in ("us", "us_ksi", "fps", "kip_ft", "kip-ft")
    rebar_sys = "us" if use_us_strength else "metric"
    cover_beam = float(beam_cover_in) / 12.0 if use_us_strength else 0.04
    cover_col = float(column_cover_in) / 12.0 if use_us_strength else 0.04
    tie_sp = float(column_tie_spacing_in) / 12.0 if use_us_strength else 0.1
    fy_k = float(rebar_fy_ksi)
    fu_k = float(rebar_fu_ksi)
    fc_psi = float(concrete_fc_psi)
    fc_mpa = float(concrete_fc_mpa)

    for cm in {beam_mat, col_mat, slab_mat}:
        model.define_material(cm, 2)
        if use_us_strength:
            model.define_concrete_normal_weight_us(cm, fc_psi)
        else:
            model.define_concrete_normal_weight_metric(cm, fc_mpa)

    beam_name = "Beam_rect"
    col_int_name = "Col_interior"
    col_ext_name = "Col_exterior"
    slab_prop = "Slab_floor"
    # Beam: SetRebarBeam → GUI "M3 Design Only (Beam)". If typelib/COM rejects the call, use plain rect.
    ok_beam = model.define_concrete_rect_beam(
        beam_name,
        beam_mat,
        rb.hb,
        rb.bw,
        rebar_long=beam_rebar_long,
        rebar_tie=beam_rebar_tie,
        cover_top=cover_beam,
        cover_bot=cover_beam,
        rebar_system=rebar_sys,
        rebar_fy_ksi=fy_k,
        rebar_fu_ksi=fu_k,
    )
    if not ok_beam:
        model.define_frame_section_rect(beam_name, beam_mat, rb.hb, rb.bw)
    # Column rebar setup may fail on some ETABS rebar size catalogs; keep geometric fallback.
    ok_col_int = model.define_concrete_rect_column(
        col_int_name,
        col_mat,
        rb.bcy,
        rb.bcx,
        rebar_long=column_rebar_long,
        rebar_tie=column_rebar_tie,
        cover=cover_col,
        tie_spacing=tie_sp,
        main_bar_size=str(column_main_bar_size),
        tie_bar_size=str(column_tie_bar_size),
        rebar_system=rebar_sys,
        rebar_fy_ksi=fy_k,
        rebar_fu_ksi=fu_k,
    )
    if not ok_col_int:
        model.define_frame_section_rect(col_int_name, col_mat, rb.bcy, rb.bcx)
    ok_col_ext = model.define_concrete_rect_column(
        col_ext_name,
        col_mat,
        rb.bcy_ext,
        rb.bcx_ext,
        rebar_long=column_rebar_long,
        rebar_tie=column_rebar_tie,
        cover=cover_col,
        tie_spacing=tie_sp,
        main_bar_size=str(column_main_bar_size),
        tie_bar_size=str(column_tie_bar_size),
        rebar_system=rebar_sys,
        rebar_fy_ksi=fy_k,
        rebar_fu_ksi=fu_k,
    )
    if not ok_col_ext:
        model.define_frame_section_rect(col_ext_name, col_mat, rb.bcy_ext, rb.bcx_ext)
    model.define_slab_section(slab_prop, slab_mat, rb.ts)

    dname = "D1"
    model.define_diaphragm(dname, is_rigid=True)

    geom = EtabsGeometry(sap_model)

    joints: List[List[List[str]]] = [
        [[None for _ in range(nxp)] for _ in range(nyp)] for _ in range(nz)
    ]
    # Grid XYZ used for frames (avoids PointObj.GetCoordCartesian COM tuple layout quirks).
    coords: List[List[List[Tuple[float, float, float]]]] = [
        [[(0.0, 0.0, 0.0) for _ in range(nxp)] for _ in range(nyp)] for _ in range(nz)
    ]
    for iz, z in enumerate(zs):
        for iy in range(nyp):
            y = iy * rb.Sy
            for ix in range(nxp):
                x = ix * rb.Sx
                name = geom.add_joint(x, y, z)
                joints[iz][iy][ix] = name
                coords[iz][iy][ix] = (float(x), float(y), float(z))

    fixed = [True] * 6
    for iy in range(nyp):
        for ix in range(nxp):
            geom.assign_restraint(joints[0][iy][ix], fixed)

    n_cols = n_beams = 0
    for iz in range(nz - 1):
        for iy in range(nyp):
            for ix in range(nxp):
                x0, y0, z0 = coords[iz][iy][ix]
                x1, y1, z1 = coords[iz + 1][iy][ix]
                prop = col_ext_name if _is_perimeter(ix, iy, nx, ny) else col_int_name
                # ETABS may return "" for auto-generated frame name; do not use truthiness.
                if geom.add_frame_by_coord(x0, y0, z0, x1, y1, z1, prop) is not None:
                    n_cols += 1

    for iz in range(1, nz):
        for iy in range(nyp):
            for ix in range(nxp - 1):
                xa, ya, za = coords[iz][iy][ix]
                xb, yb, zb = coords[iz][iy][ix + 1]
                if geom.add_frame_by_coord(xa, ya, za, xb, yb, zb, beam_name) is not None:
                    n_beams += 1
        for iy in range(nyp - 1):
            for ix in range(nxp):
                xa, ya, za = coords[iz][iy][ix]
                xb, yb, zb = coords[iz][iy + 1][ix]
                if geom.add_frame_by_coord(xa, ya, za, xb, yb, zb, beam_name) is not None:
                    n_beams += 1
        for iy in range(nyp):
            for ix in range(nxp):
                geom.assign_diaphragm_to_joint(joints[iz][iy][ix], dname)

    n_slabs = 0
    for iz in range(1, nz):
        z = float(zs[iz])
        for iy in range(ny):
            for ix in range(nx):
                x0, y0 = float(ix * rb.Sx), float(iy * rb.Sy)
                x1, y1 = float((ix + 1) * rb.Sx), float(iy * rb.Sy)
                x2, y2 = float((ix + 1) * rb.Sx), float((iy + 1) * rb.Sy)
                x3, y3 = float(ix * rb.Sx), float((iy + 1) * rb.Sy)
                nm = geom.add_shell_slab_quad(
                    x0, y0, z, x1, y1, z, x2, y2, z, x3, y3, z, slab_prop
                )
                if nm is not None:
                    n_slabs += 1

    if add_default_dead_pattern:
        load = EtabsLoading(sap_model)
        load.add_load_pattern(dead_pattern, 1, 1.0)
    # Slab shells carry self-weight via the DEAD pattern; omit beam line load (was 25*ts tributary).

    return {
        "joints": nxp * nyp * nz,
        "columns": n_cols,
        "beams": n_beams,
        "slabs": n_slabs,
        "story_sync": story_sync,
    }


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _write_csv_summary(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Parametric ETABS multi-story dataset builder")
    p.add_argument("--out", type=Path, default=Path("parametric_output"), help="Output directory")
    p.add_argument("--limit", type=int, default=0, help="Max models (0 = no limit)")
    p.add_argument("--stride", type=int, default=1, help="Take every Nth combination")
    p.add_argument("--offset", type=int, default=0, help="Skip first N combinations")
    p.add_argument("--Ly-ref", type=float, default=30.0, help="Reference plan dimension (m)")
    p.add_argument("--n-stories", type=int, default=5, help="Number of above-ground stories")
    p.add_argument("--no-etabs", action="store_true", help="Only write manifests / dry geometry (no COM)")
    p.add_argument("--manifest-only", action="store_true", help="Write parameter_manifest.json and exit")
    args = p.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    write_parameter_manifest(out / "parameter_manifest.json")

    n_total = total_combination_count()
    print(f"Total nominal combinations: {n_total}")

    if args.manifest_only:
        return 0

    rows_for_csv: List[dict] = []
    jsonl_path = out / "runs.jsonl"
    if jsonl_path.exists():
        jsonl_path.unlink()

    gen = iter_param_dicts()
    idx_global = 0
    built = 0

    for combo in gen:
        if idx_global < args.offset:
            idx_global += 1
            continue
        if args.stride > 1 and (idx_global - args.offset) % args.stride != 0:
            idx_global += 1
            continue

        rb = resolve_building(
            combo,
            Ly_ref=args.Ly_ref,
            n_stories=args.n_stories,
        )
        flat = building_to_flat_dict(rb, combo)
        flat["sample_index"] = idx_global

        if args.no_etabs:
            flat["edb_path"] = ""
            flat["status"] = "geometry_only"
            _append_jsonl(jsonl_path, flat)
            rows_for_csv.append(flat)
            built += 1
        else:
            try:
                from .etabs_api import EtabsConnection
            except ImportError:
                print("etabs_ml.etabs_api could not be imported (install with: pip install -e .).")
                return 1

            conn = EtabsConnection(attach_to_active=False)
            conn.connect()
            if not conn.sap_model:
                print("Could not attach to ETABS.")
                return 1
            try:
                stats = build_etabs_frame_model(conn.sap_model, rb)
                edb_name = out / f"model_{idx_global:05d}.edb"
                conn.sap_model.File.Save(str(edb_name))
                flat["edb_path"] = str(edb_name.resolve())
                flat["status"] = "ok"
                flat.update({f"mesh_{k}": v for k, v in stats.items()})
            except Exception as e:
                flat["edb_path"] = ""
                flat["status"] = f"error: {e}"
            finally:
                conn.close(save_model=False)

            _append_jsonl(jsonl_path, flat)
            rows_for_csv.append(flat)
            built += 1

        idx_global += 1
        if args.limit and built >= args.limit:
            break

    _write_csv_summary(out / "summary.csv", rows_for_csv)
    print(f"Wrote {built} entries to {jsonl_path} and summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
