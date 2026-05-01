"""
Unit tests (no ETABS): verify SetPresentUnits + table output helpers call the COM surface
with the expected CSI codes and Results.Setup option integers.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from etabs_ml.asce7_16_load_setup import (
    DEFAULT_ASCE716_DESIGN_SETS,
    apply_custom_response_combinations,
    asce716_load_config_from_analysis_inputs,
    parse_custom_combos_from_dict,
    should_apply_template_combos,
    should_run_asce716_combo_section,
)
from etabs_ml.csi_present_units import apply_set_present_units
from etabs_ml.csi_table_output_options import (
    apply_database_tables_output_options_for_display,
    apply_results_setup_table_output_options,
    merge_table_output_options,
)


class TestCustomCombosFromJson(unittest.TestCase):
    def test_parse_object_form(self) -> None:
        d = parse_custom_combos_from_dict(
            {"custom_combos": {"A": [["DEAD", 1.4], ["LIVE", 1.0]]}}
        )
        self.assertEqual(d["A"], [("DEAD", 1.4), ("LIVE", 1.0)])

    def test_should_run_with_custom_only(self) -> None:
        self.assertTrue(
            should_run_asce716_combo_section(
                {"apply": False, "custom_combos": {"X": [["DEAD", 1.0]]}}
            )
        )

    def test_should_run_with_etabs_defaults_only(self) -> None:
        self.assertTrue(
            should_run_asce716_combo_section(
                {"apply": False, "template_combos": False, "use_etabs_default_combos": True}
            )
        )

    def test_should_apply_template_explicit_false(self) -> None:
        self.assertFalse(
            should_apply_template_combos({"apply": True, "template_combos": False})
        )

    def test_apply_custom_calls_com(self) -> None:
        sm = MagicMock()
        sm.LoadPatterns.GetNameList.return_value = (2, ["DEAD", "LIVE"])
        sm.RespCombo.GetNameList.return_value = (0, [])
        sm.RespCombo.Add.return_value = 0
        sm.RespCombo.SetCaseList.return_value = 0
        log = apply_custom_response_combinations(
            sm, {"MY": [("DEAD", 1.2), ("LIVE", 1.0)]}, replace_existing=False
        )
        self.assertTrue(log["ok"])
        sm.RespCombo.Add.assert_called_once()


class TestAsce716LoadConfigFromJson(unittest.TestCase):
    def test_apply_false_returns_none(self) -> None:
        self.assertIsNone(
            asce716_load_config_from_analysis_inputs(
                {"apply": False},
                dead="D",
                live="L",
                super_dead="S",
                seismic_x="QX",
                seismic_y="QY",
            )
        )

    def test_apply_true_builds_config(self) -> None:
        cfg = asce716_load_config_from_analysis_inputs(
            {"apply": True, "design_sets": ["concrete_frame"]},
            dead="DEAD",
            live="LIVE",
            super_dead="SDL",
            seismic_x="QX",
            seismic_y="QY",
        )
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.design_sets, ("concrete_frame",))
        self.assertEqual(cfg.dead, "DEAD")
        self.assertFalse(cfg.include_wind)

    def test_defaults_design_sets_when_missing(self) -> None:
        cfg = asce716_load_config_from_analysis_inputs(
            {"apply": True},
            dead="D",
            live="L",
            super_dead="S",
            seismic_x="QX",
            seismic_y="QY",
        )
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.design_sets, DEFAULT_ASCE716_DESIGN_SETS)


class TestApplySetPresentUnits(unittest.TestCase):
    def test_empty_skips_com(self) -> None:
        sm = MagicMock()
        out = apply_set_present_units(sm, None, product="etabs", quiet=True)
        self.assertFalse(out["applied"])
        sm.SetPresentUnits.assert_not_called()

    def test_kn_m_c_calls_code_6(self) -> None:
        sm = MagicMock()
        sm.SetPresentUnits.return_value = 0
        out = apply_set_present_units(sm, "kN_m_C", product="etabs", quiet=True)
        sm.SetPresentUnits.assert_called_once_with(6)
        self.assertTrue(out["applied"])
        self.assertEqual(out["resolved"], "kN_m_C")
        self.assertEqual(out["code"], 6)

    def test_kip_ft_f_calls_code_4(self) -> None:
        sm = MagicMock()
        sm.SetPresentUnits.return_value = 0
        out = apply_set_present_units(sm, "kip_ft_F", product="etabs", quiet=True)
        sm.SetPresentUnits.assert_called_once_with(4)
        self.assertTrue(out["applied"])


class TestApplyResultsSetupTableOptions(unittest.TestCase):
    def _sap_with_setup(self) -> MagicMock:
        sm = MagicMock()
        setup = MagicMock()
        for name in (
            "SetOptionBaseReactLoc",
            "SetOptionModeShape",
            "SetOptionBucklingMode",
            "SetOptionMultiStepStatic",
            "SetOptionNLStatic",
            "SetOptionMultiValuedCombo",
            "SetOptionDirectHist",
            "SetOptionModalHist",
        ):
            getattr(setup, name).return_value = 0
        sm.Results.Setup = setup
        dt = MagicMock()
        dt.SetTableOutputOptionsForDisplay.return_value = 0
        sm.DatabaseTables = dt
        return sm

    def test_results_setup_invokes_expected_setters(self) -> None:
        sm = self._sap_with_setup()
        apply_results_setup_table_output_options(
            sm,
            {"multistep_static": 2},
            quiet=True,
        )
        setup = sm.Results.Setup
        setup.SetOptionMultiStepStatic.assert_called_once_with(2)
        setup.SetOptionNLStatic.assert_called_once()
        setup.SetOptionModalHist.assert_called_once()
        sm.DatabaseTables.SetTableOutputOptionsForDisplay.assert_called_once()

    def test_merge_overrides_defaults(self) -> None:
        m = merge_table_output_options({"multistep_static": 2})
        self.assertEqual(m["multistep_static"], 2)
        self.assertEqual(m["nonlinear_static"], 1)


class TestDatabaseTablesOutputOptions(unittest.TestCase):
    def test_prefers_set_table_output_options_for_display(self) -> None:
        sm = MagicMock()
        dt = MagicMock()
        dt.SetTableOutputOptionsForDisplay.return_value = 0
        sm.DatabaseTables = dt
        apply_database_tables_output_options_for_display(
            sm, {"multistep_static": 3}, quiet=True
        )
        dt.SetTableOutputOptionsForDisplay.assert_called_once()
        args = dt.SetTableOutputOptionsForDisplay.call_args[0]
        self.assertEqual(args[12], 3, "MultistepStatic index 12 in newer ETABS signature")

    def test_falls_back_to_set_output_options_for_display(self) -> None:
        sm = MagicMock()
        dt = MagicMock()
        del dt.SetTableOutputOptionsForDisplay
        dt.SetOutputOptionsForDisplay = MagicMock(return_value=0)
        sm.DatabaseTables = dt
        apply_database_tables_output_options_for_display(sm, None, quiet=True)
        dt.SetOutputOptionsForDisplay.assert_called_once()


if __name__ == "__main__":
    unittest.main()
