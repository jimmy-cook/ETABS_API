"""
Minimal ``DatabaseTables`` wrapper for CSI ``GetTableForDisplayArray`` → pandas.

Replaces the historical ``etabs_api/database.py`` dependency used by
``structured_analysis_export``.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd

from .etabs_api import _csi_ret0


def _flatten_table_rows(fields: Sequence[str], flat: Sequence[Any]) -> List[List[Any]]:
    n = len(fields)
    if n == 0 or not flat:
        return []
    return [list(flat[i : i + n]) for i in range(0, len(flat), n)]


class DatabaseTables:
    """Read ETABS database tables into pandas (via ``SapModel.DatabaseTables``)."""

    def __init__(self, SapModel: Any = None, *, EtabsObject: Any = None) -> None:
        self.SapModel = SapModel
        if SapModel is None and EtabsObject is not None:
            self.SapModel = getattr(EtabsObject, "SapModel", None) or EtabsObject
        if self.SapModel is None:
            raise TypeError("DatabaseTables requires SapModel= or EtabsObject=")

    def read(self, table_key: str, *, to_dataframe: bool = True, **_: Any) -> Optional[pd.DataFrame]:
        dt = self.SapModel.DatabaseTables
        ret: Any = None
        last_err: Optional[BaseException] = None
        for args in (
            (str(table_key), [], "", 0, [], 0, []),
            (str(table_key), [], "", 0, []),
        ):
            try:
                ret = dt.GetTableForDisplayArray(*args)
                break
            except Exception as exc:  # pragma: no cover - version-dependent arity
                last_err = exc
                ret = None
        if ret is None:
            if last_err is not None:
                raise last_err
            return pd.DataFrame() if to_dataframe else None

        if not isinstance(ret, (list, tuple)) or len(ret) < 5:
            return pd.DataFrame() if to_dataframe else None
        if _csi_ret0(ret[-1]) != 0:
            return pd.DataFrame() if to_dataframe else None

        fields = tuple(str(x) for x in ret[2])
        flat = list(ret[4])
        rows = _flatten_table_rows(fields, flat)
        if not to_dataframe:
            return None  # type: ignore[return-value]
        return pd.DataFrame(rows, columns=list(fields))
