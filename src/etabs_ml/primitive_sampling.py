"""
Space-filling + discrete grid sampling for primitive ML dataset generation.

- method "lhs": Latin Hypercube in [0,1]^d per batch, scaled to continuous bounds.
- method "random": independent uniform draws.

Optional: ``discrete["h_story_m"]`` as a list of floats (e.g. three floor-to-floor
heights in metres). FPS-friendly configs may use aliases such as ``h_story_ft``,
``Sx_ft``, ``hb_in``, and ``ts_in_abs``; they are normalized to metre keys when
the config is loaded. When story height is discrete, those values are cycled with
``(nx, ny, n_stories)`` and any ``continuous["h_story_m"]`` entry is ignored.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence, Tuple

import numpy as np

from .primitive_resolve import PrimitiveSample, resolve_from_primitives

METRES_PER_FOOT = 0.3048
METRES_PER_INCH = 0.0254


CONTINUOUS_KEYS: Sequence[str] = (
    "Sx_m",
    "Sy_m",
    "h_story_m",
    "hb_m",
    "bw_over_hb",
    "bcx_m",
    "bcy_m",
    "ts_m",
    "col_top_area_ratio",
    "col_ext_area_ratio",
)


def load_config(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise ImportError("Install PyYAML to use .yaml configs: pip install pyyaml") from e
        return normalize_length_unit_aliases(yaml.safe_load(text))
    return normalize_length_unit_aliases(json.loads(text))


def _convert_sequence(values: Any, factor: float) -> List[float]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"Expected a list/tuple of lengths, got {type(values).__name__}")
    return [float(x) * factor for x in values]


def _convert_bound_spec(spec: Any, factor: float) -> Dict[str, float]:
    if not isinstance(spec, dict):
        raise TypeError(f"Expected a min/max bound spec, got {type(spec).__name__}")
    return {"min": float(spec["min"]) * factor, "max": float(spec["max"]) * factor}


def _convert_band(values: Any, factor: float) -> List[float]:
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise TypeError("Expected a two-value constraint band")
    return [float(values[0]) * factor, float(values[1]) * factor]


def _copy_converted(
    section: Dict[str, Any],
    source_key: str,
    target_key: str,
    factor: float,
    converter,
) -> None:
    if source_key in section:
        if target_key in section:
            raise ValueError(f"Config has both {source_key!r} and {target_key!r}; keep one.")
        section[target_key] = converter(section.pop(source_key), factor)


def normalize_length_unit_aliases(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize FPS-friendly length aliases to the internal metre-based schema.

    The generator resolves primitives and constraints in metres. This lets a US/FPS
    JSON stay readable in feet/inches while preserving the existing downstream code.
    """
    if not isinstance(cfg, dict):
        return cfg

    disc = cfg.get("discrete")
    if isinstance(disc, dict):
        _copy_converted(disc, "h_story_ft", "h_story_m", METRES_PER_FOOT, _convert_sequence)
        _copy_converted(disc, "h_story_in", "h_story_m", METRES_PER_INCH, _convert_sequence)

    cont = cfg.get("continuous")
    if isinstance(cont, dict):
        for src, dst in (
            ("Sx_ft", "Sx_m"),
            ("Sy_ft", "Sy_m"),
            ("h_story_ft", "h_story_m"),
        ):
            _copy_converted(cont, src, dst, METRES_PER_FOOT, _convert_bound_spec)
        for src, dst in (
            ("hb_in", "hb_m"),
            ("bcx_in", "bcx_m"),
            ("bcy_in", "bcy_m"),
            ("ts_in", "ts_m"),
        ):
            _copy_converted(cont, src, dst, METRES_PER_INCH, _convert_bound_spec)

    cons = cfg.get("constraints")
    if isinstance(cons, dict):
        for src, dst in (
            ("Smax_ft", "Smax_m"),
            ("h_story_ft", "h_story_m"),
        ):
            _copy_converted(cons, src, dst, METRES_PER_FOOT, _convert_band)
        for src, dst in (
            ("hb_in", "hb_m"),
            ("bw_in", "bw_m"),
            ("bc_min_in", "bc_min_m"),
            ("ts_in_abs", "ts_m_abs"),
        ):
            _copy_converted(cons, src, dst, METRES_PER_INCH, _convert_band)

    return cfg


def _scale_unit(u: float, spec: Dict[str, Any]) -> float:
    lo, hi = float(spec["min"]), float(spec["max"])
    return lo + u * (hi - lo)


def lhs_unit_matrix(n_rows: int, n_dim: int, rng: np.random.Generator) -> np.ndarray:
    """Classic LHS: n_rows samples in [0,1]^n_dim."""
    h = np.zeros((n_rows, n_dim), dtype=np.float64)
    for j in range(n_dim):
        cuts = np.linspace(0.0, 1.0, n_rows + 1)
        u = rng.random(n_rows)
        pts = u * (cuts[1:] - cuts[:-1]) + cuts[:-1]
        perm = rng.permutation(n_rows)
        h[:, j] = pts[perm]
    return h


def _continuous_keys_in_order(continuous_cfg: Dict[str, Any]) -> Tuple[str, ...]:
    """Subset of CONTINUOUS_KEYS that appear in ``continuous_cfg`` (stable order)."""
    return tuple(k for k in CONTINUOUS_KEYS if k in continuous_cfg)


def draw_continuous_row(
    method: str,
    row_index: int,
    batch_H: np.ndarray,
    continuous_cfg: Dict[str, Any],
    rng: np.random.Generator,
    continuous_keys: Sequence[str],
) -> Dict[str, float]:
    n_dim = len(continuous_keys)
    if n_dim == 0:
        return {}
    if method == "lhs":
        u = batch_H[row_index % len(batch_H)]
    else:
        u = rng.random(n_dim)
    out: Dict[str, float] = {}
    for j, key in enumerate(continuous_keys):
        spec = continuous_cfg[key]
        out[key] = _scale_unit(float(u[j]), spec)
    return out


def iter_discrete_combos(discrete_cfg: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    nxs = discrete_cfg["nx"]
    nys = discrete_cfg["ny"]
    nss = discrete_cfg["n_stories"]
    hs = discrete_cfg.get("h_story_m")
    if isinstance(hs, (list, tuple)) and len(hs) > 0:
        hs_f = [float(x) for x in hs]
        return list(itertools.product(nxs, nys, nss, hs_f))
    return list(itertools.product(nxs, nys, nss))


def build_primitive(
    nx: int,
    ny: int,
    n_stories: int,
    cont: Dict[str, float],
) -> PrimitiveSample:
    hb = cont["hb_m"]
    bw = cont["bw_over_hb"] * hb
    return PrimitiveSample(
        nx=int(nx),
        ny=int(ny),
        n_stories=int(n_stories),
        Sx_m=cont["Sx_m"],
        Sy_m=cont["Sy_m"],
        h_story_m=cont["h_story_m"],
        hb_m=hb,
        bw_m=bw,
        bcx_m=cont["bcx_m"],
        bcy_m=cont["bcy_m"],
        ts_m=cont["ts_m"],
        col_top_area_ratio=cont["col_top_area_ratio"],
        col_ext_area_ratio=cont["col_ext_area_ratio"],
    )


def sample_stream(
    cfg: Dict[str, Any],
    *,
    rng: np.random.Generator,
) -> Iterator[Tuple[PrimitiveSample, int]]:
    """
    Yields (PrimitiveSample, draw_index) until caller stops.
    draw_index increments every attempt (including rejects upstream).
    """
    samp = cfg["sampling"]
    method = str(samp.get("method", "lhs")).lower()
    disc = cfg["discrete"]
    raw_cont = cfg["continuous"]
    hs_disc = disc.get("h_story_m")
    if isinstance(hs_disc, (list, tuple)) and len(hs_disc) > 0:
        cont_cfg = {k: v for k, v in raw_cont.items() if k != "h_story_m"}
    else:
        cont_cfg = dict(raw_cont)
    combos = iter_discrete_combos(disc)
    continuous_keys = _continuous_keys_in_order(cont_cfg)
    n_dim = len(continuous_keys)
    batch_size = int(samp.get("lhs_batch_size", 2048))
    batch_H = (
        lhs_unit_matrix(batch_size, n_dim, rng)
        if method == "lhs" and n_dim > 0
        else np.zeros((max(batch_size, 1), max(n_dim, 1)), dtype=np.float64)
    )

    k = 0
    while True:
        combo = combos[k % len(combos)]
        if len(combo) == 4:
            nx, ny, nst, h_story_disc = combo
            h_story_disc = float(h_story_disc)
        else:
            nx, ny, nst = combo
            h_story_disc = None
        cont = draw_continuous_row(method, k, batch_H, cont_cfg, rng, continuous_keys)
        if h_story_disc is not None:
            cont["h_story_m"] = h_story_disc
        prim = build_primitive(nx, ny, nst, cont)
        yield prim, k
        k += 1
        if method == "lhs" and k % batch_size == 0:
            batch_H = lhs_unit_matrix(batch_size, n_dim, rng)
