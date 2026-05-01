#!/usr/bin/env python3
"""
CSI OAPI present unit systems (GetPresentUnits / SetPresentUnits).

ETABS supports six bundled systems; SAP2000 exposes additional metric variants.

String ids (e.g. ``kN_m_C``) map to CSI ``eUnits`` codes used by ``SapModel.SetPresentUnits``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, FrozenSet, Optional

# Must match universal_etabs_extractor_complete unit_values keys
ETABS_PRESENT_UNITS: FrozenSet[str] = frozenset(
    {
        "lb_in_F",
        "lb_ft_F",
        "kip_in_F",
        "kip_ft_F",
        "kN_mm_C",
        "kN_m_C",
    }
)

# Must match universal_sap2000_extractor SAP2000_UNIT_MAP values
SAP2000_PRESENT_UNITS: FrozenSet[str] = frozenset(
    {
        "lb_in_F",
        "lb_ft_F",
        "kip_in_F",
        "kip_ft_F",
        "kN_mm_C",
        "kN_m_C",
        "kgf_mm_C",
        "kgf_m_C",
        "N_mm_C",
        "N_m_C",
        "Ton_mm_C",
        "Ton_m_C",
        "kN_cm_C",
        "kgf_cm_C",
        "N_cm_C",
        "Ton_cm_C",
    }
)

DEFAULT_PRESENT_UNITS = "kip_ft_F"

# CSI SapModel.SetPresentUnits integer (same mapping as etabs_api.etabs_obj.EtabsModel.enum_units)
PRESENT_UNITS_TO_SETPRESENTUNITS_CODE: Dict[str, int] = {
    "lb_in_F": 1,
    "lb_ft_F": 2,
    "kip_in_F": 3,
    "kip_ft_F": 4,
    "kN_mm_C": 5,
    "kN_m_C": 6,
    "kgf_mm_C": 7,
    "kgf_m_C": 8,
    "N_mm_C": 9,
    "N_m_C": 10,
    "Ton_mm_C": 11,
    "Ton_m_C": 12,
    "kN_cm_C": 13,
    "kgf_cm_C": 14,
    "N_cm_C": 15,
    "Ton_cm_C": 16,
}


def normalize_csi_product(name: str) -> str:
    n = (name or "etabs").strip().lower().replace(" ", "")
    if n in ("sap2k", "sap_2000", "sap2000"):
        return "sap2000"
    return "etabs"


def allowed_present_units_for_rag_product(rag_product_normalized: str) -> FrozenSet[str]:
    """``rag_product_normalized``: ``'etabs'`` | ``'sap2000'`` (see ``normalize_csi_product``)."""
    if rag_product_normalized == "sap2000":
        return SAP2000_PRESENT_UNITS
    return ETABS_PRESENT_UNITS


def _getenv_present_units() -> str:
    """First non-empty env among standard names (no external package)."""
    for key in ("ETABS_PRESENT_UNITS", "CSI_PRESENT_UNITS", "AI_MODEL_EXPLORER_PRESENT_UNITS"):
        v = os.getenv(key, "").strip()
        if v:
            return v
    return ""


def resolve_present_units(
    rag_product_normalized: str,
    client_value: Optional[str],
) -> str:
    """
    Return a valid CSI present-units id for the active app.
    Falls back to env (see ``_getenv_present_units``) then ``DEFAULT_PRESENT_UNITS``.
    """
    allowed = allowed_present_units_for_rag_product(rag_product_normalized)
    env_raw = _getenv_present_units()
    fallback = DEFAULT_PRESENT_UNITS if DEFAULT_PRESENT_UNITS in allowed else sorted(allowed)[0]
    env_pick = env_raw if env_raw in allowed else fallback

    if client_value is None or str(client_value).strip() == "":
        return env_pick

    v = str(client_value).strip()
    if v in allowed:
        return v
    return env_pick


def present_units_set_code(units_id: str, *, product: str = "etabs") -> Optional[int]:
    """Map resolved string id to ``SetPresentUnits`` integer, or None if unknown."""
    key = resolve_present_units(normalize_csi_product(product), units_id)
    return PRESENT_UNITS_TO_SETPRESENTUNITS_CODE.get(key)


def apply_set_present_units(
    sap_model: Any,
    units_id: Optional[str],
    *,
    product: str = "etabs",
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    Call ``SapModel.SetPresentUnits`` using a string id (``kN_m_C``, ``kip_ft_F``, …).

    If ``units_id`` is None or empty, does nothing (keeps the model's current units).
    """
    out: Dict[str, Any] = {"applied": False, "requested": units_id, "resolved": None, "code": None, "ret": None}
    if units_id is None or str(units_id).strip() == "":
        return out
    resolved = resolve_present_units(normalize_csi_product(product), units_id)
    code = PRESENT_UNITS_TO_SETPRESENTUNITS_CODE.get(resolved)
    out["resolved"] = resolved
    out["code"] = code
    if code is None:
        if not quiet:
            print(f"[WARN] csi_present_units: no SetPresentUnits code for {resolved!r}")
        return out
    try:
        sap_model.SetModelIsLocked(False)
    except Exception:
        pass
    try:
        ret = sap_model.SetPresentUnits(int(code))
        out["ret"] = ret
        ok = True
        try:
            if isinstance(ret, (list, tuple)) and ret:
                ok = int(ret[0]) == 0
            elif ret is not None:
                ok = int(ret) == 0
        except (TypeError, ValueError):
            ok = True
        out["applied"] = bool(ok)
        if not quiet:
            tag = "OK" if ok else "WARN"
            print(f"[{tag}] SetPresentUnits({code}) [{resolved}] ret={ret!r}")
    except Exception as exc:
        out["error"] = str(exc)
        if not quiet:
            print(f"[WARN] SetPresentUnits failed: {exc}")
    return out
