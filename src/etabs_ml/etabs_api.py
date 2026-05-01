import math
import sys
import csv

from ctypes import c_double

from comtypes import BSTR
from comtypes.safearray import _midlSAFEARRAY

try:
    import comtypes.client
    import comtypes.gen
except ImportError:
    print("Error: The 'comtypes' package is missing. Please install it using 'pip install comtypes'")
    sys.exit(1)


def _csi_api_v1_c_helper():
    """
    comtypes names the generated package csiAPIv1 or CSiAPIv1 depending on
    typelib / platform; QueryInterface needs the matching module.
    """
    g = comtypes.gen
    for mod_name in ("csiAPIv1", "CSiAPIv1"):
        if hasattr(g, mod_name):
            mod = getattr(g, mod_name)
            if hasattr(mod, "cHelper"):
                return mod.cHelper
    raise AttributeError(
        "comtypes.gen has no csiAPIv1/CSiAPIv1 with cHelper. "
        "Install ETABS and ensure its API typelib is registered."
    )


def _csi_ret0(val):
    """CSI/COM helpers often return ``int`` or ``[ret, ...]``; first element is status (0 = ok)."""
    if val is None:
        return 0
    if isinstance(val, (list, tuple)) and len(val) > 0:
        try:
            return int(val[0])
        except (TypeError, ValueError):
            return -1
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


def _csi_ret_last(val):
    """Like ``_csi_ret0`` but use the **last** element when COM returns ``(..., pRetVal)``."""
    if isinstance(val, (list, tuple)) and len(val) > 0:
        return _csi_ret0(val[-1])
    return _csi_ret0(val)


def _named_mass_source_set_ok(ret) -> bool:
    """``SourceMass.SetMassSource`` / ``MassDef.SetMassSource`` may return ``int`` or ``(LoadPat, SF, pRetVal)``."""
    if isinstance(ret, (list, tuple)):
        if len(ret) == 3:
            return _csi_ret0(ret[2]) == 0
        if len(ret) >= 1:
            return _csi_ret_last(ret) == 0
    return _csi_ret0(ret) == 0


def _yn(b: bool) -> str:
    return "Yes" if b else "No"


def _mass_table_cell(v) -> str:
    if v is None:
        return ""
    return str(v)


def _prop_material_mass_option(add_elements: bool, add_masses: bool, use_loads: bool) -> int:
    """CSI ``MyOption`` for ``cPropMaterial.SetMassSource``: 1=self+added; 2=loads; 3=self+added+loads."""
    if use_loads and add_elements:
        return 3
    if use_loads:
        return 2
    return 1


def _sort_mass_load_pairs_super_dead_first(
    load_patterns: list,
    multipliers: list,
) -> tuple[list, list]:
    """Put SDL / SUPERDEAD before LIVE so the legacy API order matches typical input."""
    pairs = list(zip(load_patterns, multipliers))

    def sort_key(item):
        name = str(item[0]).upper()
        if name == "SDL" or "SUPER" in name or name == "SUPERDEAD":
            return (0, name)
        if name in ("LIVE", "L") or name.startswith("LIVE"):
            return (1, name)
        return (2, name)

    pairs.sort(key=sort_key)
    return [p[0] for p in pairs], [float(p[1]) for p in pairs]


def _prop_material_set_mass_source_csi(
    sap_model,
    *,
    add_elements: bool,
    add_masses: bool,
    use_loads: bool,
    load_patterns: list,
    multipliers: list,
) -> bool:
    """
    ``SapModel.PropMaterial.SetMassSource(MyOption, NumberLoads, LoadPat, SF)`` (CSI ETABSv1).

    ``LoadPat`` / ``SF`` are ``[in, out]`` **SAFEARRAY** parameters; plain Python lists do not
    marshal correctly — use ``_midlSAFEARRAY(BSTR)`` / ``_midlSAFEARRAY(c_double)``.
    ``MyOption`` = 3 matches the usual GUI: element self mass + mass from specified loads.
    """
    pm = getattr(sap_model, "PropMaterial", None) or getattr(sap_model, "propmaterial", None)
    if pm is None:
        return False

    my_opt = _prop_material_mass_option(add_elements, add_masses, use_loads)
    lp = [str(x) for x in load_patterns]
    mul = [float(x) for x in multipliers]
    if use_loads:
        lp, mul = _sort_mass_load_pairs_super_dead_first(lp, mul)
        if len(mul) != len(lp):
            mul = [1.0] * len(lp)
        n = len(lp)
    else:
        n = 0
        lp = []
        mul = []

    sa_bstr = _midlSAFEARRAY(BSTR)
    sa_dbl = _midlSAFEARRAY(c_double)
    try:
        load_arr = sa_bstr.create(lp)
        sf_arr = sa_dbl.create(mul)
    except Exception:
        return False

    fn = getattr(pm, "SetMassSource", None)
    if callable(fn):
        try:
            ret = fn(int(my_opt), int(n), load_arr, sf_arr)
            if isinstance(ret, (list, tuple)):
                if len(ret) >= 1 and _csi_ret_last(ret) == 0:
                    return True
            elif _csi_ret0(ret) == 0:
                return True
        except Exception:
            pass

    fn1 = getattr(pm, "SetMassSource_1", None)
    if not callable(fn1):
        return False
    try:
        load_arr2 = sa_bstr.create(lp)
        sf_arr2 = sa_dbl.create(mul)
        r = fn1(
            bool(add_elements),
            bool(add_masses),
            bool(use_loads),
            int(n),
            load_arr2,
            sf_arr2,
        )
        return _csi_ret_last(r) == 0
    except Exception:
        return False


def _read_mass_source_definition(sap_model):
    """Return ``(fields_tuple, list_of_rows)`` from ``Mass Source Definition`` table, or ``(None, None)``."""
    try:
        ret = sap_model.DatabaseTables.GetTableForDisplayArray(
            "Mass Source Definition", [], "", 0, [], 0, []
        )
    except Exception:
        return None, None
    if not isinstance(ret, (list, tuple)) or len(ret) < 5:
        return None, None
    if _csi_ret0(ret[-1]) != 0:
        return None, None
    fields = tuple(str(x) for x in ret[2])
    data = list(ret[4])
    n = len(fields)
    if n == 0 or not data:
        return fields, []
    rows = [list(data[i : i + n]) for i in range(0, len(data), n)]
    return fields, rows


def _apply_mass_source_definition_rows(sap_model, fields: tuple, rows: list) -> bool:
    """Write ``Mass Source Definition`` via ``SetTableForEditingArray`` + ``ApplyEditedTables``."""
    if not fields or not rows:
        return False
    flat = []
    for r in rows:
        if len(r) != len(fields):
            return False
        flat.extend(_mass_table_cell(x) for x in r)
    try:
        sap_model.SetModelIsLocked(False)
    except Exception:
        pass
    try:
        tret = sap_model.DatabaseTables.SetTableForEditingArray(
            "Mass Source Definition", 0, list(fields), 0, flat
        )
        if isinstance(tret, (list, tuple)) and len(tret) > 0:
            if _csi_ret0(tret[-1]) != 0:
                return False
        elif _csi_ret0(tret) != 0:
            return False
    except Exception:
        return False
    try:
        aret = sap_model.DatabaseTables.ApplyEditedTables(True)
        n_fatal = int(aret[0]) if isinstance(aret, (list, tuple)) and len(aret) > 0 else 0
        return n_fatal == 0
    except Exception:
        return False


def _try_source_mass_set_patterns(sap_model, existing_name: str, ms_name: str,
                                   add_elements: bool, add_masses: bool, use_loads: bool,
                                   lp: list, mul: list, is_default: bool) -> bool:
    """
    Attempt ``SourceMass.SetMassSource`` using the *existing* mass source name so ETABS can
    locate and update the record (avoids the -99 that occurs when the target name does not
    yet exist).  Tries ``existing_name`` first, then ``ms_name`` as a fallback.
    """
    n = len(lp) if use_loads else 0
    pats = lp if use_loads else []
    muls = mul if use_loads else []
    try:
        sap_model.SetModelIsLocked(False)
    except Exception:
        pass
    for coll_attr in ("SourceMass", "sourceMass", "MassDef", "massDef"):
        coll = getattr(sap_model, coll_attr, None)
        if coll is None:
            continue
        fn = getattr(coll, "SetMassSource", None)
        if not callable(fn):
            continue
        for name_try in dict.fromkeys([existing_name, ms_name]):  # dedup, preserve order
            if not name_try:
                continue
            try:
                sa_bstr = _midlSAFEARRAY(BSTR)
                sa_dbl = _midlSAFEARRAY(c_double)
                load_arr = sa_bstr.create(pats)
                sf_arr = sa_dbl.create(muls)
                ret = fn(
                    str(name_try),
                    bool(add_elements),
                    bool(add_masses),
                    bool(use_loads),
                    bool(is_default),
                    int(n),
                    load_arr,
                    sf_arr,
                )
                if _named_mass_source_set_ok(ret):
                    return True
            except Exception:
                continue
    return False


def _define_mass_source_via_database(
    sap_model,
    ms_name: str,
    add_elements: bool,
    add_masses: bool,
    add_loads: bool,
    load_patterns: list,
    multipliers: list,
    *,
    is_default: bool = True,
    inc_lateral: bool = True,
    inc_vertical: bool = False,
    lump_mass: bool = True,
) -> bool:
    """
    Two-phase mass-source writer.

    **Phase 1 — COM (preferred, persists correctly across save/reload):**
    Read the *existing* default mass source name from the database table (e.g. ``MsSrc1``),
    then call ``SourceMass.SetMassSource`` with that existing name so ETABS can find and
    update the record.  If that succeeds, update the **Mass Source Definition** table to
    rename the mass source to ``ms_name`` and set the flag columns.

    **Phase 2 — table-only fallback (flags persist; patterns are in-session only):**
    When COM still fails, write both the header row and inline ``LoadPattern``/``Multiplier``
    rows via ``SetTableForEditingArray`` + ``ApplyEditedTables``.  The flags (name, SourceSelf,
    SourceLoads …) persist to the ``.edb``; the pattern rows are readable in-session via the
    global ``PropMaterial.SetMassSource`` path (which does persist).
    """
    fields, rows = _read_mass_source_definition(sap_model)
    if not fields or not rows:
        return False

    lp = [str(x) for x in load_patterns if x is not None and str(x).strip()]
    mul = [float(x) for x in multipliers]
    use_loads = bool(add_loads and lp)
    if use_loads and len(mul) != len(lp):
        mul = [1.0] * len(lp)

    # Resolve the existing default mass source name (before any rename).
    existing_ms_name: str = ""
    for r in rows:
        d = dict(zip(fields, r))
        nm = str(d.get("Name", "")).strip()
        if nm:
            if str(d.get("IsDefault", "")).strip().lower() == "yes":
                existing_ms_name = nm
                break
    if not existing_ms_name and rows:
        existing_ms_name = str(dict(zip(fields, rows[0])).get("Name", "")).strip()

    # --- Phase 1: PropMaterial global mass (always attempt; required for "loads and elements") ---
    pm_ok = _prop_material_set_mass_source_csi(
        sap_model,
        add_elements=bool(add_elements),
        add_masses=bool(add_masses),
        use_loads=use_loads,
        load_patterns=lp,
        multipliers=mul,
    )
    if use_loads and not pm_ok:
        return False

    # --- Phase 2: Try COM SourceMass.SetMassSource using the existing name ---
    # Using the existing name (e.g. "MsSrc1") lets ETABS locate the record and update it
    # in its internal store, which persists correctly on File.Save.
    source_mass_com_ok = False
    if use_loads and lp:
        source_mass_com_ok = _try_source_mass_set_patterns(
            sap_model, existing_ms_name, ms_name,
            bool(add_elements), bool(add_masses), use_loads, lp, mul, bool(is_default),
        )

    # --- Phase 3: Database table — rename mass source and set flag columns ---
    new_rows: list = []
    updated = False
    for i, r in enumerate(rows):
        d = dict(zip(fields, r))
        nm = str(d.get("Name", "")).strip()
        match = (not updated) and (
            len(rows) == 1
            or nm == existing_ms_name
            or nm.lower() in ("mssrc1", "msrc1", "default")
            or nm == str(ms_name).strip()
            or (str(d.get("IsDefault", "")).strip() == "Yes" and i == 0)
        )
        if match:
            updated = True
            d["Name"] = str(ms_name).strip()
            d["IsDefault"] = _yn(bool(is_default))
            d["IncLateral"] = _yn(bool(inc_lateral))
            d["IncVertical"] = _yn(bool(inc_vertical))
            d["LumpMass"] = _yn(bool(lump_mass))
            d["SourceSelf"] = _yn(bool(add_elements))
            d["SourceAdded"] = _yn(bool(add_masses))
            d["SourceLoads"] = _yn(bool(use_loads))
            d["MoveMass"] = str(d.get("MoveMass", "No") or "No")
        new_rows.append([_mass_table_cell(d.get(f)) for f in fields])

    if not any(str(r[0]).strip() == str(ms_name).strip() for r in new_rows):
        d = dict(zip(fields, rows[0]))
        d["Name"] = str(ms_name).strip()
        d["IsDefault"] = _yn(bool(is_default))
        d["IncLateral"] = _yn(bool(inc_lateral))
        d["IncVertical"] = _yn(bool(inc_vertical))
        d["LumpMass"] = _yn(bool(lump_mass))
        d["SourceSelf"] = _yn(bool(add_elements))
        d["SourceAdded"] = _yn(bool(add_masses))
        d["SourceLoads"] = _yn(bool(use_loads))
        d["MoveMass"] = "No"
        new_rows[0] = [_mass_table_cell(d.get(f)) for f in fields]

    # If COM path succeeded the patterns are already stored internally; we only need
    # to apply the header row to rename the mass source.  When COM failed, also append
    # inline LoadPattern/Multiplier rows so ETABS can reconstruct them in-session.
    if not source_mass_com_ok and use_loads and lp and "LoadPattern" in fields and "Multiplier" in fields:
        for pat, sf in zip(lp, mul):
            pat_d: dict = {f: "" for f in fields}
            pat_d["Name"] = str(ms_name).strip()
            pat_d["LoadPattern"] = str(pat)
            pat_d["Multiplier"] = str(sf)
            new_rows.append([_mass_table_cell(pat_d.get(f)) for f in fields])

    return _apply_mass_source_definition_rows(sap_model, fields, new_rows)


def _propframe_iface(sap_model):
    """``cPropFrame`` is exposed as ``SapModel.propframe`` (see CSI examples)."""
    return getattr(sap_model, "propframe", None) or getattr(sap_model, "PropFrame")


class EtabsConnection:
    """Manages connection to the ETABS application."""
    def __init__(self, attach_to_active=True):
        self.helper = None
        self.etabs_object = None
        self.sap_model = None
        self.attach_to_active = attach_to_active

    def connect(self):
        try:
            self.helper = comtypes.client.CreateObject("csiAPIv1.Helper")
            self.helper = self.helper.QueryInterface(_csi_api_v1_c_helper())
        except (OSError, comtypes.COMError):
            print("Cannot create a new instance of the Helper object. Ensure ETABS is properly installed/registered.")
            sys.exit(-1)
            
        if self.attach_to_active:
            try:
                self.etabs_object = comtypes.client.GetActiveObject("CSI.ETABS.API.ETABSObject")
                self.sap_model = self.etabs_object.SapModel
                print("Successfully attached to the active ETABS instance.")
            except (OSError, comtypes.COMError):
                print("No active ETABS instance found. Starting a new one...")
                self._start_new_instance()
        else:
            self._start_new_instance()

    def _start_new_instance(self):
        try:
            self.etabs_object = self.helper.CreateObjectProgID("CSI.ETABS.API.ETABSObject")
            self.etabs_object.ApplicationStart()
            self.sap_model = self.etabs_object.SapModel
            print("Successfully started a new ETABS instance.")
        except (OSError, comtypes.COMError):
            print("Failed to start new ETABS instance. Ensure ETABS is installed and licensed.")
            sys.exit(-1)

    def close(self, save_model=False):
        if self.etabs_object:
            self.etabs_object.ApplicationExit(save_model)
            self.sap_model = None
            self.etabs_object = None


class EtabsModel:
    """Manages Model creation, Materials, Sections, and structural definitions."""
    def __init__(self, sap_model):
        self.sap_model = sap_model
        
    def initialize_new_model(self, units=6):
        # CSI eUnits code (see etabs_units.UNIT_PRESETS); default 6 = kN, m, °C.
        ret = self.sap_model.InitializeNewModel(units)
        return ret == 0
        
    def create_blank_model(self):
        ret = self.sap_model.File.NewBlank()
        return ret == 0
        
    def define_material(self, name, mat_type=1):
        # mat_type: 1 = Steel, 2 = Concrete
        ret = self.sap_model.PropMaterial.SetMaterial(name, mat_type)
        return ret == 0

    def define_concrete_normal_weight_us(self, name: str, fc_psi: float) -> bool:
        """
        Normal-weight concrete after ``SetMaterial(name, 2)`` for **US customary** models
        (e.g. kip-ft-°F). ``fc_psi`` is f'c in **psi** (4000, 5000, …). Sets Ec via ACI-style
        sqrt curve, then ``SetOConcrete`` and a typical 150 pcf weight.
        """
        fc = float(fc_psi)
        ec_psi = 57000.0 * math.sqrt(max(fc, 250.0))
        nu = 0.2
        therm = 5.5e-6
        try:
            _csi_ret0(self.sap_model.PropMaterial.SetMPIsotropic(name, ec_psi, nu, therm))
        except (TypeError, ValueError, AttributeError):
            pass
        r = self.sap_model.PropMaterial.SetOConcrete(
            name, fc, False, 1.0, 2, 4, 0.002219, 0.005
        )
        if _csi_ret0(r) != 0:
            return False
        try:
            wm = self.sap_model.PropMaterial.SetWeightAndMass(name, 1, 150.0)
            return _csi_ret0(wm) == 0
        except (TypeError, ValueError, AttributeError):
            return True

    def define_concrete_normal_weight_metric(self, name: str, fc_mpa: float) -> bool:
        """
        Normal-weight concrete after ``SetMaterial(name, 2)`` for **MKS / SI** models
        (kN-m-°C or N-mm-°C). ``fc_mpa`` is f'c in **MPa** (e.g. 30, 40). Ec ≈ 4700√f'c (MPa),
        then ``SetOConcrete`` and mass density typical for normal-weight concrete.
        """
        fc = float(fc_mpa)
        ec = 4700.0 * math.sqrt(max(fc, 1.0))
        nu = 0.2
        therm = 1.0e-5
        try:
            _csi_ret0(self.sap_model.PropMaterial.SetMPIsotropic(name, ec, nu, therm))
        except (TypeError, ValueError, AttributeError):
            pass
        r = self.sap_model.PropMaterial.SetOConcrete(
            name, fc, False, 1.0, 2, 4, 0.002219, 0.005
        )
        if _csi_ret0(r) != 0:
            return False
        try:
            wm = self.sap_model.PropMaterial.SetWeightAndMass(name, 1, 2500.0)
            return _csi_ret0(wm) == 0
        except (TypeError, ValueError, AttributeError):
            return True

    def ensure_rebar_steel(
        self,
        name: str,
        *,
        system: str = "metric",
        fy_ksi: float = 60.0,
        fu_ksi: float = 90.0,
    ) -> bool:
        """
        Define reinforcing steel (type 6) if missing.

        ``system``:
          - ``metric`` / ``mks`` / ``si`` — fy/fu scale ~400/600 MPa style (CSI examples).
          - ``us`` / ``us_ksi`` / ``fps`` — ASTM Grades in **ksi** (e.g. fy=60, fu=90 for A615 Gr60).
        """
        sys_key = str(system).strip().lower()
        try:
            names = list(self.sap_model.PropMaterial.GetNameList()[1])
        except (TypeError, IndexError, AttributeError):
            names = []
        if name in names:
            return True
        if _csi_ret0(self.sap_model.PropMaterial.SetMaterial(name, 6)) != 0:
            return False

        if sys_key in ("us", "us_ksi", "fps", "kip_ft", "kip-ft"):
            fy_i = int(round(float(fy_ksi)))
            fu_i = int(round(float(fu_ksi)))
            fye_i = int(round(1.25 * fy_i))
            fue_i = int(round(1.25 * fu_i))
            tuples = (
                (name, fy_i, fu_i, fye_i, fue_i, 1, 1, 0.01, 0.09, 0, 0),
                (name, fy_i, fu_i, fye_i, fue_i, 1, 1, 0.01, 0.09, False, 0),
                (name, fy_i, fu_i, fye_i, fue_i, 1, 1, 0.01, 0.09, False),
            )
        else:
            # MKS / SI / default: MPa-scale rebar (CSI-style examples).
            tuples = (
                (name, 400, 600, 500, 750, 1, 1, 0.01, 0.09, 0, 0),
                (name, 400, 600, 500, 750, 1, 1, 0.01, 0.09, False, 0),
                (name, 400, 600, 500, 750, 1, 1, 0.01, 0.09, False),
            )
        for args in tuples:
            try:
                r = self.sap_model.PropMaterial.SetORebar(*args)
                if _csi_ret0(r) == 0:
                    return True
            except (TypeError, ValueError, AttributeError):
                continue
        return True

    def define_frame_section_rect(self, name, material_name, depth, width):
        """
        Generic rectangular concrete/steel **geometry** only. For **concrete** beams, ETABS
        may default the concrete-design dialog to *column* (P–M2–M3) until
        ``SetRebarBeam`` is applied; use :meth:`define_concrete_rect_beam` for beams.
        """
        ret = self.sap_model.PropFrame.SetRectangle(name, material_name, depth, width)
        return _csi_ret0(ret) == 0

    def define_concrete_rect_beam(
        self,
        name: str,
        material_name: str,
        depth: float,
        width: float,
        *,
        rebar_long: str = "A615Gr60",
        rebar_tie: str = "A615Gr60",
        cover_top: float = 0.04,
        cover_bot: float = 0.04,
        rebar_system: str = "metric",
        rebar_fy_ksi: float = 60.0,
        rebar_fu_ksi: float = 90.0,
    ) -> bool:
        """
        Rectangular **concrete beam** with **M3 design only (beam)** reinforcement mode:
        calls ``cPropFrame.SetRebarBeam`` after ``SetRectangle`` (not ``SetRebarColumn``).
        *cover_* values are in **current ETABS length units** (e.g. m for MKS, ft for kip-ft).
        """
        for nm in (rebar_long, rebar_tie):
            if not self.ensure_rebar_steel(
                nm,
                system=rebar_system,
                fy_ksi=rebar_fy_ksi,
                fu_ksi=rebar_fu_ksi,
            ):
                return False
        if not self.define_frame_section_rect(name, material_name, depth, width):
            return False
        pf = _propframe_iface(self.sap_model)
        try:
            ret = pf.SetRebarBeam(
                name,
                rebar_long,
                rebar_tie,
                float(cover_top),
                float(cover_bot),
                0,
                0,
                0,
                0,
            )
        except (TypeError, ValueError, AttributeError):
            # Some COM typelib builds use different SetRebarBeam signatures; caller may fall back
            # to a plain rectangle section (see run_parametric_dataset.build_etabs_frame_model).
            return False
        return _csi_ret0(ret) == 0

    def define_concrete_rect_column(
        self,
        name: str,
        material_name: str,
        depth: float,
        width: float,
        *,
        rebar_long: str = "A615Gr60",
        rebar_tie: str = "A615Gr60",
        cover: float = 0.04,
        n_bars_3dir: int = 3,
        n_bars_2dir: int = 3,
        main_bar_size: str = "#5",
        tie_bar_size: str = "#3",
        tie_spacing: float = 0.1,
        n_tie_2dir: int = 2,
        n_tie_3dir: int = 2,
        design: bool = False,
        rebar_system: str = "metric",
        rebar_fy_ksi: float = 60.0,
        rebar_fu_ksi: float = 90.0,
    ) -> bool:
        """
        Rectangular **concrete column** reinforcement mode:
        calls ``cPropFrame.SetRebarColumn`` after ``SetRectangle``.
        """
        for nm in (rebar_long, rebar_tie):
            if not self.ensure_rebar_steel(
                nm,
                system=rebar_system,
                fy_ksi=rebar_fy_ksi,
                fu_ksi=rebar_fu_ksi,
            ):
                return False
        if not self.define_frame_section_rect(name, material_name, depth, width):
            return False
        pf = _propframe_iface(self.sap_model)
        try:
            ret = pf.SetRebarColumn(
                name,
                rebar_long,
                rebar_tie,
                1,
                1,
                float(cover),
                0,
                int(n_bars_3dir),
                int(n_bars_2dir),
                str(main_bar_size),
                str(tie_bar_size),
                float(tie_spacing),
                int(n_tie_2dir),
                int(n_tie_3dir),
                bool(design),
            )
        except (TypeError, ValueError, AttributeError):
            return False
        return _csi_ret0(ret) == 0

    def define_slab_section(
        self,
        name: str,
        material_name: str,
        thickness_m: float,
        slab_type: int = 5,
        shell_type: int = 2,
    ) -> bool:
        """
        Floor-type shell slab (same pattern as etabs_api.area PropArea.SetSlab).
        slab_type / shell_type: CSI enums (defaults match typical flat slab + shell).
        """
        ret = self.sap_model.PropArea.SetSlab(
            name, slab_type, shell_type, material_name, float(thickness_m)
        )
        return ret == 0

    def define_diaphragm(self, name, is_rigid=True):
        ret = self.sap_model.Diaphragm.SetDiaphragm(name, is_rigid)
        return ret == 0

    def define_mass_source(self, name="MS_EXPORT", add_elements=True, add_masses=False, add_loads=True, is_default=True, load_patterns=None, multipliers=None):
        """
        Define a named mass source (``Define → Mass Source`` in ETABS).

        ``add_elements`` — element self mass; ``add_masses`` — additional mass;
        ``add_loads`` — specified load patterns with ``load_patterns`` / ``multipliers``.

        **Named mass source:** ``SapModel.SourceMass.SetMassSource`` (CSI ``cMassSource``).
        Some third-party snippets incorrectly use ``PropMassSource`` — that property is
        not on the published ``cSapModel`` typelib; use ``SourceMass`` instead.
        ``LoadPat`` / ``SF`` are marshalled as **SAFEARRAY** (plain ``list`` often yields
        ``-99`` from ETABS).

        The COM ``IsDefault`` argument follows ``is_default``. When ``is_default`` is true,
        ``SourceMass.SetDefault`` is also called when available.

        If direct COM ``SetMassSource`` fails (often ``-99`` on some builds), the model is
        updated via the **Mass Source Definition** database table so the requested name
        (e.g. ``MS1``) appears under ``Define → Mass Source``, plus ``PropMaterial`` for
        load-pattern mass factors. Persist changes with ``File.Save`` on the ``.edb`` path
        before closing ETABS so the next GUI session reads the updated table from disk.

        """
        ms_name = str(name or "MS_EXPORT").strip() or "MS_EXPORT"
        lp = [str(x) for x in (load_patterns or []) if x is not None and str(x).strip()]
        mul = [float(x) for x in (multipliers or [])]
        if add_loads and lp:
            if len(mul) != len(lp):
                mul = [1.0] * len(lp)
        else:
            add_loads = False
            lp = []
            mul = []
        n_loads = len(lp)

        def _try_set_named_mass_source(coll) -> bool:
            fn = getattr(coll, "SetMassSource", None)
            if not callable(fn):
                return False
            try:
                sa_bstr = _midlSAFEARRAY(BSTR)
                sa_dbl = _midlSAFEARRAY(c_double)
                load_arr = sa_bstr.create(lp) if n_loads else sa_bstr.create([])
                sf_arr = sa_dbl.create(mul) if n_loads else sa_dbl.create([])
                ret = fn(
                    ms_name,
                    bool(add_elements),
                    bool(add_masses),
                    bool(add_loads),
                    bool(is_default),
                    int(n_loads),
                    load_arr,
                    sf_arr,
                )
            except TypeError:
                return False
            except Exception:
                return False
            if not _named_mass_source_set_ok(ret):
                return False
            if is_default:
                sdef = getattr(coll, "SetDefault", None)
                if callable(sdef):
                    try:
                        _csi_ret0(sdef(ms_name))
                    except Exception:
                        pass
            return True

        try:
            self.sap_model.SetModelIsLocked(False)
        except Exception:
            pass
        for coll_name in (
            "PropMassSource",
            "propMassSource",
            "SourceMass",
            "sourceMass",
            "MassDef",
            "massDef",
        ):
            coll = getattr(self.sap_model, coll_name, None)
            if coll is None:
                continue
            if _try_set_named_mass_source(coll):
                return True
        return _define_mass_source_via_database(
            self.sap_model,
            ms_name,
            bool(add_elements),
            bool(add_masses),
            bool(add_loads),
            lp,
            mul,
            is_default=bool(is_default),
        )


class EtabsGeometry:
    """Manages creation of geometry like joints, frames, boundaries, and properties assignment."""
    def __init__(self, sap_model):
        self.sap_model = sap_model
        
    def add_joint(self, x, y, z):
        res = self.sap_model.PointObj.AddCartesian(x, y, z)
        # comtypes may return [Ret, Name] or [Name, Ret]; Ret is 0 on success.
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            if res[0] == 0:
                return res[1]
            if res[1] == 0:
                return res[0]
        return None

    def add_frame_by_coord(self, x1, y1, z1, x2, y2, z2, prop_name="Default"):
        res = self.sap_model.FrameObj.AddByCoord(x1, y1, z1, x2, y2, z2, "", prop_name, "")
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            if res[0] == 0:
                return res[1]
            if res[1] == 0:
                return res[0]
        return None

    def add_shell_slab_quad(
        self,
        x0: float,
        y0: float,
        z0: float,
        x1: float,
        y1: float,
        z1: float,
        x2: float,
        y2: float,
        z2: float,
        x3: float,
        y3: float,
        z3: float,
        prop_name: str,
    ):
        """Four-point floor panel in Global XYZ; returns area object name or None."""
        n = 4
        xs = [x0, x1, x2, x3]
        ys = [y0, y1, y2, y3]
        zs = [z0, z1, z2, z3]
        ret = self.sap_model.AreaObj.AddByCoord(n, xs, ys, zs, "", prop_name)
        if not isinstance(ret, (list, tuple)):
            return None
        if len(ret) > 3 and ret[3]:
            return ret[3]
        if len(ret) >= 2:
            if ret[0] == 0 and isinstance(ret[1], str):
                return ret[1]
            if ret[1] == 0 and isinstance(ret[0], str):
                return ret[0]
        return None

    def assign_restraint(self, joint_name, dof_array):
        # dof_array is [U1, U2, U3, R1, R2, R3] (booleans)
        ret = self.sap_model.PointObj.SetRestraint(joint_name, dof_array)
        return ret == 0

    def assign_diaphragm_to_joint(self, joint_name, diaphragm_name, axis=2):
        # axis 2 is usually Global Z for standard orthogonal models
        ret = self.sap_model.PointObj.SetDiaphragm(joint_name, axis, diaphragm_name)
        return ret == 0


class EtabsLoading:
    """Manages load patterns, static cases, wind, dynamic cases, combinations, and assignments."""
    def __init__(self, sap_model):
        self.sap_model = sap_model
        
    def add_load_pattern(self, name, load_type=1, self_weight_multiplier=0):
        # load_type: LTYPE_DEAD = 1, LTYPE_LIVE = 3
        ret = self.sap_model.LoadPatterns.Add(name, load_type, self_weight_multiplier, True)
        if isinstance(ret, (list, tuple)) and len(ret) > 0:
            try:
                return int(ret[0]) == 0
            except (TypeError, ValueError):
                return False
        try:
            return int(ret) == 0
        except (TypeError, ValueError):
            return False

    def add_auto_wind_pattern(self, name, code="ASCE 7-16"):
        # load_type 8 = Wind
        ret = self.sap_model.LoadPatterns.Add(name, 8, 0, True)
        if ret == 0:
            ret_auto = self.sap_model.LoadPatterns.SetAutoWindCode(name, code)
            return ret_auto == 0
        return False

    def add_auto_seismic_pattern(self, name, code="ASCE 7-16"):
        """Seismic load pattern (type 5) + ``SetAutoSeismicCode`` when the API exposes it."""
        ret = self.sap_model.LoadPatterns.Add(name, 5, 0, True)
        if ret != 0:
            return False
        fn = getattr(self.sap_model.LoadPatterns, "SetAutoSeismicCode", None)
        if fn is None:
            return True
        try:
            return int(fn(name, code)) == 0
        except Exception:
            return False

    def assign_area_uniform_load(self, area_name, load_pattern_name, pressure, direction=6):
        """Uniform surface load on one area (present force/area units)."""
        d = int(direction)
        a = str(area_name)
        p = str(load_pattern_name)
        f = float(pressure)
        try:
            ret = self.sap_model.AreaObj.SetLoadUniform(a, p, f, d)
            return int(ret) == 0
        except Exception:
            try:
                # Five-arg: load value + global (0) + direction (6 = often global Z in ETABS).
                ret = self.sap_model.AreaObj.SetLoadUniform(a, p, f, 0, d)
                return int(ret) == 0
            except Exception:
                try:
                    ret = self.sap_model.AreaObj.SetLoadUniform(
                        a, p, f, "Global", d, True, 0, False
                    )
                    return int(ret) == 0
                except Exception:
                    return False

    def add_load_combination(self, combo_name, combo_type=0, load_patterns=None, multipliers=None):
        # combo_type: 0=Linear Add, 1=Envelope, 2=Absolute Add, 3=SRSS, 4=Range Add
        ret = self.sap_model.RespCombo.Add(combo_name, combo_type)
        if ret == 0 and load_patterns and multipliers:
            for p, m in zip(load_patterns, multipliers):
                 # 0 = LoadCase (treat pattern as case)
                 self.sap_model.RespCombo.SetCaseList(combo_name, 0, p, m)
        return ret == 0
        
    def assign_joint_force(self, joint_name, load_pattern_name, forces):
        # forces is [F1, F2, F3, M1, M2, M3]
        ret = self.sap_model.PointObj.SetLoadForce(joint_name, load_pattern_name, forces, True, "Global", 0)
        return ret == 0

    def assign_frame_dist_load(self, frame_name, load_pattern_name, dir_id, mag_start, mag_end, is_relative=True):
        # dir_id: 1=Local-1, 2=Local-2, 3=Local-3, 4=X, 5=Y, 6=Z... 10=Gravity
        ret = self.sap_model.FrameObj.SetLoadDistributed(
            frame_name, load_pattern_name, 1, dir_id,
            0, 1, mag_start, mag_end, "Global", is_relative, True, 0
        )
        return ret == 0


class EtabsAnalysis:
    """Manages the analysis phase and modal case configurations."""
    def __init__(self, sap_model):
        self.sap_model = sap_model
        
    def modify_modal_case(self, case_name="MODAL", max_modes=12, min_modes=1):
        # Creates modal case if it doesn't exist, converts to Eigen if it wasn't
        ret = self.sap_model.LoadCases.ModalEigen.SetCase(case_name)
        if ret == 0:
            self.sap_model.LoadCases.ModalEigen.SetNumberModes(case_name, max_modes, min_modes)
        return ret == 0

    def run_analysis(self):
        print("Running analysis...")
        # ETABS may leave some load cases unchecked (e.g. modal); without this,
        # RunAnalysis can skip eigen/modal while still running static cases.
        try:
            self.sap_model.Analyze.SetRunCaseFlag("all_load_cases", True, True)
        except TypeError:
            try:
                self.sap_model.Analyze.SetRunCaseFlag("all_load_cases", True)
            except Exception:
                pass
        except Exception:
            pass
        ret = self.sap_model.Analyze.RunAnalysis()
        if ret == 0:
            print("Analysis completed successfully.")
        else:
            print("Warning: Analysis finished with non-zero exit code.")
        return ret == 0
        
    def select_load_case_for_output(self, case_name):
        self.sap_model.Results.Setup.DeselectAllCasesAndCombosForOutput()
        ret = self.sap_model.Results.Setup.SetCaseSelectedForOutput(case_name)
        return ret == 0
        
    def select_load_combo_for_output(self, combo_name):
        self.sap_model.Results.Setup.DeselectAllCasesAndCombosForOutput()
        ret = self.sap_model.Results.Setup.SetComboSelectedForOutput(combo_name)
        return ret == 0


class EtabsResults:
    """Extracts required input/output after analysis and provides export utility."""
    def __init__(self, sap_model):
        self.sap_model = sap_model

    def get_joint_displacements(self, joint_name="All"):
        item_type = 1 if joint_name.lower() == "all" else 0
        res = self.sap_model.Results.JointDispl(joint_name, item_type)
        if res[0] == 0:
            NumRes = res[1]
            Obj = res[2]
            ACase = res[4]
            U1 = res[7]
            U2 = res[8]
            U3 = res[9]
            R1 = res[10]
            R2 = res[11]
            R3 = res[12]
            
            displacements_list = []
            for i in range(NumRes):
                displacements_list.append({
                    "Joint": Obj[i], "LoadCase": ACase[i],
                    "U1": U1[i], "U2": U2[i], "U3": U3[i],
                    "R1": R1[i], "R2": R2[i], "R3": R3[i]
                })
            return displacements_list
        return []

    def get_frame_forces(self, frame_name="All"):
        item_type = 1 if frame_name.lower() == "all" else 0
        res = self.sap_model.Results.FrameForce(frame_name, item_type)
        if res[0] == 0:
            NumRes = res[1]
            Obj = res[2]
            ACase = res[4]
            Station = res[6]
            P = res[7]; V2 = res[8]; V3 = res[9]
            T = res[10]; M2 = res[11]; M3 = res[12]

            forces_list = []
            for i in range(NumRes):
                forces_list.append({
                    "Frame": Obj[i], "Station": Station[i], "LoadCase": ACase[i],
                    "P": P[i], "V2": V2[i], "V3": V3[i],
                    "T": T[i], "M2": M2[i], "M3": M3[i]
                })
            return forces_list
        return []

    def get_base_reactions(self, point_name="All"):
        item_type = 1 if point_name.lower() == "all" else 0
        res = self.sap_model.Results.JointReact(point_name, item_type)
        if res[0] == 0:
            NumRes = res[1]
            Obj = res[2]
            ACase = res[4]
            F1 = res[7]; F2 = res[8]; F3 = res[9]
            M1 = res[10]; M2 = res[11]; M3 = res[12]
            
            data = []
            for i in range(NumRes):
                data.append({
                    "Joint": Obj[i], "LoadCase": ACase[i],
                    "F1": F1[i], "F2": F2[i], "F3": F3[i],
                    "M1": M1[i], "M2": M2[i], "M3": M3[i]
                })
            return data
        return []

    def get_modal_periods(self):
        # Make sure MODAL is selected for output before calling this, or all cases
        res = self.sap_model.Results.ModalPeriod()
        if res[0] == 0:
            NumRes = res[1]
            StepNum = res[5]
            Period = res[6]
            Frequency = res[7]
            
            data = []
            for i in range(NumRes):
                data.append({
                    "ModeNumber": StepNum[i],
                    "Period": Period[i],
                    "Frequency": Frequency[i]
                })
            return data
        return []

    def export_to_csv(self, data_list, filename):
        """Exports a list of dictionaries to a CSV file."""
        if not data_list:
            print(f"No data to export for {filename}.")
            return False
        keys = data_list[0].keys()
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(data_list)
        print(f"Data exported successfully to {filename}")
        return True
