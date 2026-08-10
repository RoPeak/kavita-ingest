from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def test_disposable_kavita_comic_flexible_scan() -> None:
    database_value = os.environ.get("KAVITA_LIVE_DB")
    if not database_value:
        pytest.skip("set KAVITA_LIVE_DB to a stopped disposable Kavita database")
    database = Path(database_value)
    assert database.is_file()

    connection = sqlite3.connect(database)
    rows = list(
        connection.execute(
            """
            select s.Name, v.Name, ch.Number, ch.Range, ch.IsSpecial, m.FileName
            from MangaFile m
            join Chapter ch on ch.Id = m.ChapterId
            join Volume v on v.Id = ch.VolumeId
            join Series s on s.Id = v.SeriesId
            order by s.Name, m.FileName
            """
        )
    )
    connection.close()

    series_names = {row[0] for row in rows}
    assert "Absolute Batman (2024)" in series_names
    assert "Absolute Batman (2031)" in series_names
    fractional = next(row for row in rows if row[5].endswith("#70.5"))
    assert fractional[2:5] == ("70.5", "70.5", 0)

    projected_cases = [row for row in rows if row[0] == "Projection Cases (2024)"]
    assert len(projected_cases) == 6
    assert all(row[1] == "100000" and row[2] == "-100000" and row[4] == 1 for row in projected_cases)
    assert any("Annual 02" in row[5] for row in projected_cases)
    assert any("SP01 - Special" in row[5] for row in projected_cases)
    assert any("SP01 - One Shot" in row[5] for row in projected_cases)
    assert any("v01 - Trade One" in row[5] for row in projected_cases)
    assert any("v02 - Omnibus Two" in row[5] for row in projected_cases)
    assert any("SPTPB1 - Symbolic Trade" in row[5] for row in projected_cases)
