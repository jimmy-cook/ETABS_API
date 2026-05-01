"""
Correct ETABS OAPI usage for **auto wind** (read before copying snippets from the web).

Common mistakes
---------------
- ``eLoadPatternType``: **5 = Quake (seismic)**, **6 = Wind**. Using 5 for a wind
  pattern breaks auto-wind and lateral load type checks.
- The COM method is ``SapModel.LoadPatterns.AutoWind.SetASCE716`` — not
  ``SetASCE7-16`` (invalid Python identifier).
- **ASCE 7-16** autowind in U.S. models typically expects **wind speed in mph**;
  for **m/s**, convert or use ``Asce716WindASCE716Params.wind_speed_unit = "m_s"``
  in ``asce7_16_load_setup.py``.

This script only demonstrates the call sequence; it does not start ETABS.
"""

from __future__ import annotations

# Reuse the same numbers as the generated CSI typelib (see comtypes gen CSiAPIv1)
ELOADPATTERN_WIND = 6  # eLoadPatternType_Wind
ELOADPATTERN_QUAKE = 5  # eLoadPatternType_Quake (not for wind!)


def example_asce716_autowind(sap_model, pattern_name: str = "WIND_Y") -> int:
    """
    Full **AutoWind.SetASCE716** call (all positional args required by the typelib).
    Return value: 0 = success in typical CSI documentation; -99 = failure on some builds.
    """
    ret = sap_model.LoadPatterns.Add(pattern_name, ELOADPATTERN_WIND, 0.0, True)
    if int(ret[0] if isinstance(ret, (list, tuple)) else ret) != 0:
        return -1

    # ExposureFrom, DirAngle, Cpw, Cpl, ASCECase, ASCEe1, ASCEe2, UserZ, TopZ, BottomZ,
    # WindSpeed, ExposureType, Kzt, GustFactor, Kd, SolidGrossRatio, UserExposure
    return sap_model.LoadPatterns.AutoWind.SetASCE716(
        pattern_name,
        1,  # exposure_from: 1=diaphragm extents, 2=areas, 3=frames
        90.0,  # dir angle (deg), e.g. 90 for global +Y
        0.8,  # Cpw
        -0.5,  # Cpl
        1,  # ASCE case
        0.0,  # ASCEe1
        0.0,  # ASCEe2
        False,  # UserZ
        0.0,  # TopZ
        0.0,  # BottomZ
        115.0,  # wind speed (mph for typical ASCE 7-16 in ETABS)
        2,  # exposure type (B/C/D per CHM)
        1.0,  # Kzt
        0.85,  # gust G
        0.85,  # Kd
        0.2,  # solid/gross
        False,  # user exposure
    )


def example_eurocode_2005_autowind(sap_model, pattern_name: str = "WIND_Y") -> int:
    """Use **SetEurocode12005** for Eurocode 1-4 (EN) auto-wind, not SetASCE716."""
    sap_model.LoadPatterns.Add(pattern_name, ELOADPATTERN_WIND, 0.0, True)
    return sap_model.LoadPatterns.AutoWind.SetEurocode12005(
        pattern_name,
        1,
        90.0,
        0.8,
        -0.5,
        False,
        0.0,
        0.0,
        25.0,  # wind speed (confirm model/SI in CSI CHM)
        0,  # terrain
        1.0,  # orography
        1.0,  # k1
        1.0,  # CsCd
        False,  # user exposure
    )
