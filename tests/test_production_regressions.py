from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel
from kavita_ingest.comicinfo import PLANNED_COMICINFO_PROFILE
from kavita_ingest.config import AppConfig
from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind, SequenceNumber, SourceFormat, SourceRecord
from kavita_ingest.planning import default_planning_policy
from kavita_ingest.planning_service import PlanBuilder, _writer_versions


def _comic_identity(
    *,
    series: str = "Doomsday Clock",
    title: str = "That Annihilated Place",
    number: str = "1",
    year: int = 2017,
) -> CanonicalIdentity:
    return CanonicalIdentity(
        MediaKind.COMIC,
        title,
        ("Geoff Johns",),
        series_title=series,
        sequence=SequenceNumber.parse(number),
        run_start_year=year,
        item_type="issue",
        publisher="DC Comics",
        release_date="2017-11-22",
        release_date_precision="day",
        resolution=ResolutionLevel.MANUAL,
    )


def _builder(tmp_path: Path) -> tuple[PlanBuilder, sqlite3.Connection]:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    connection = connect(database)
    return PlanBuilder(connection, AppConfig(database_path=database)), connection



def test_new_cbz_plans_freeze_explicit_comicinfo_compatibility_profile() -> None:
    versions = _writer_versions(SourceFormat.CBZ)

    assert versions["comicinfo_schema"] == "2.1"
    assert versions["comicinfo_profile"] == PLANNED_COMICINFO_PROFILE

def test_planner_routes_comic_pdf_to_pdf_safe_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder, connection = _builder(tmp_path)
    monkeypatch.setattr(
        "kavita_ingest.planning_service._writer_versions",
        lambda source_format: {"fixture": source_format.value},
    )
    source = SourceRecord(
        tmp_path / "Doomsday Clock #1.pdf",
        123,
        456,
        "a" * 64,
        SourceFormat.PDF,
        "pdf",
    )

    try:
        projection, transformations, versions = builder._project(
            _comic_identity(), source, default_planning_policy()
        )
    finally:
        connection.close()

    assert transformations == ({"type": "pdf_comic_metadata"},)
    assert versions == {"fixture": "pdf"}
    assert projection.filename == "Doomsday Clock (2017) - 001 - That Annihilated Place.pdf"
    assert projection.ownership.clear_fields == ()
    assert projection.ownership.set_fields["series"] == "Doomsday Clock (2017)"
    assert projection.ownership.set_fields["title"] == "That Annihilated Place"
    assert projection.ownership.set_fields["authors"] == ["Geoff Johns"]
    assert "series_index" not in projection.ownership.set_fields
    assert "Series" not in projection.ownership.set_fields
    assert "Number" not in projection.ownership.set_fields


def test_planner_routes_zip_container_with_cbr_suffix_to_cbz_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder, connection = _builder(tmp_path)
    monkeypatch.setattr(
        "kavita_ingest.planning_service._writer_versions",
        lambda source_format: {"fixture": source_format.value},
    )
    source = SourceRecord(
        tmp_path / "Invincible Universe - Battle Beast 001.cbr",
        123,
        456,
        "b" * 64,
        SourceFormat.CBZ,
        "zip",
    )

    try:
        projection, transformations, versions = builder._project(
            _comic_identity(
                series="Invincible Universe: Battle Beast",
                title="Invincible Universe: Battle Beast",
                year=2025,
            ),
            source,
            default_planning_policy(),
        )
    finally:
        connection.close()

    assert transformations == ({"type": "zip_comic_to_cbz"},)
    assert versions == {"fixture": "cbz"}
    assert projection.filename.endswith(".cbz")
    assert projection.metadata["Series"] == "Invincible Universe: Battle Beast (2025)"


def test_standalone_collection_can_plan_without_fabricated_sequence() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Supergirl: Woman of Tomorrow",
        ("Tom King",),
        series_title="Supergirl: Woman of Tomorrow",
        item_type="collected-edition",
        publisher="DC Comics",
        resolution=ResolutionLevel.MANUAL,
    )

    assert identity.planning_blocks() == ()
