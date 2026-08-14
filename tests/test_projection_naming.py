from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel, work_only_identity
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.naming import NamingPolicy, detect_collisions
from kavita_ingest.projection import (
    OwnershipManifest,
    project_book,
    project_comic,
    project_comic_pdf,
)


@pytest.mark.parametrize(
    ("raw", "number", "rendered"),
    [
        ("1", "1", "001"),
        ("001", "1", "001"),
        ("0.5", "0.5", "0.5"),
        ("70.5", "70.5", "70.5"),
        ("1A", "1A", "1A"),
        ("1-5", "1-5", "1-5"),
        ("TPB1", "TPB1", "TPB1"),
    ],
)
def test_comic_projection_preserves_flexible_number(raw: str, number: str, rendered: str) -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "",
        (),
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse(raw),
        run_start_year=2024,
        item_type="issue",
    )
    projection = project_comic(identity)
    assert identity.series_title == "Absolute Batman"
    assert projection.metadata == {
        "Series": "Absolute Batman (2024)",
        "Number": number,
        "Volume": "",
        "Format": "",
        "Title": "",
    }
    assert projection.filename == f"Absolute Batman (2024) - {rendered}.cbz"
    assert projection.destination_folder == PurePosixPath("Absolute Batman (2024)")


@pytest.mark.parametrize(
    ("item_type", "comic_format"),
    [
        ("annual", "Annual"),
        ("special", "Special"),
        ("one-shot", "One-Shot"),
        ("trade", "Trade Paperback"),
        ("omnibus", "Omnibus"),
        ("graphic-novel", "Graphic Novel"),
    ],
)
def test_special_formats_have_filename_identity_and_no_implicit_volume(
    item_type: str, comic_format: str
) -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Deluxe",
        (),
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1A"),
        run_start_year=1986,
        item_type=item_type,
    )
    projection = project_comic(identity)
    assert projection.metadata["Series"] == "Watchmen (1986)"
    assert projection.metadata["Number"] == "1A"
    assert projection.metadata["Volume"] == ""
    assert projection.metadata["Format"] == comic_format
    assert projection.destination_folder.name == "Specials"
    assert "1A - Deluxe" in projection.filename


def test_v1_comic_flexible_target_keeps_run_year_in_series_not_volume() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Abomination, Conclusion",
        (),
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        run_start_year=2024,
        item_type="issue",
    )

    projection = project_comic(identity)

    assert identity.series_title == "Absolute Batman"
    assert identity.run_start_year == 2024
    assert projection.metadata["Series"] == "Absolute Batman (2024)"
    assert projection.metadata["Volume"] == ""
    assert "Volume" in projection.ownership.clear_fields
    assert projection.destination_folder == PurePosixPath("Absolute Batman (2024)")
    assert projection.filename.startswith("Absolute Batman (2024) - 014")


def test_integer_collection_volume_is_explicit_and_separate_from_run_year() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Book One",
        (),
        series_title="Saga",
        sequence=SequenceNumber.parse("TPB1"),
        run_start_year=2012,
        item_type="collected-edition",
        collection_volume=1,
        resolution=ResolutionLevel.MANUAL,
    )
    projection = project_comic(identity)
    assert projection.metadata["Volume"] == 1
    assert projection.metadata["Series"] == "Saga (2012)"
    assert not identity.provider_identity


def test_comic_projection_emits_only_resolved_rich_comicinfo_metadata() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "The Zoo",
        ("Scott Snyder",),
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        run_start_year=2024,
        item_type="issue",
        publisher="DC Comics",
        release_date="2025-11-26",
        release_date_precision="day",
        cover_date="2026-01",
        cover_date_precision="month",
        language="en-US",
        identifiers={"comic_vine": "4000-140001"},
        contributors={
            "writers": ("Scott Snyder",),
            "pencillers": ("Nick Dragotta",),
            "inkers": ("Inky Person",),
            "colorists": ("Colour Person",),
            "letterers": ("Letter Person",),
            "cover_artists": ("Cover Person",),
            "editors": ("Editor Person",),
            "translators": ("Translator Person",),
        },
    )
    metadata = project_comic(identity).metadata
    assert metadata == {
        "Series": "Absolute Batman (2024)",
        "Number": "14",
        "Volume": "",
        "Format": "",
        "Title": "The Zoo",
        "Writer": "Scott Snyder",
        "Penciller": "Nick Dragotta",
        "Inker": "Inky Person",
        "Colorist": "Colour Person",
        "Letterer": "Letter Person",
        "CoverArtist": "Cover Person",
        "Editor": "Editor Person",
        "Translator": "Translator Person",
        "Publisher": "DC Comics",
        "Year": 2025,
        "Month": 11,
        "Day": 26,
        "LanguageISO": "en-US",
    }
    assert "GTIN" not in metadata


@pytest.mark.parametrize(
    ("value", "precision", "expected"),
    [
        ("2024-02-29", "day", {"Year": 2024, "Month": 2, "Day": 29}),
        ("2026", "year", {}),
        ("2026-02", "month", {}),
        ("2026-13", "day", {}),
        ("2025-02-29", "day", {}),
        ("unknown", "day", {}),
    ],
)
def test_comic_projection_uses_only_an_exact_release_date(
    value: str, precision: str, expected: dict[str, int]
) -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Issue",
        (),
        series_title="Series",
        sequence=SequenceNumber.parse("1"),
        run_start_year=2024,
        item_type="issue",
        release_date=value,
        release_date_precision=precision,
    )
    projection = project_comic(identity)
    metadata = projection.metadata
    assert {key: metadata[key] for key in ("Year", "Month", "Day") if key in metadata} == expected
    preserved = set(projection.ownership.preserve_fields)
    if expected:
        assert not {"Year", "Month", "Day"} & preserved
    else:
        assert {"Year", "Month", "Day"} <= preserved


@pytest.mark.parametrize(
    ("cover_date", "precision"), [("2026-01", "month"), ("2026", "year"), (None, None)]
)
def test_cover_date_never_manufactures_comicinfo_release_fields(
    cover_date: str | None, precision: str | None
) -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Issue",
        (),
        series_title="Series",
        sequence=SequenceNumber.parse("1"),
        run_start_year=2024,
        item_type="issue",
        cover_date=cover_date,
        cover_date_precision=precision,
    )

    projection = project_comic(identity)

    assert not {"Year", "Month", "Day"} & projection.metadata.keys()
    assert {"Year", "Month", "Day"} <= set(projection.ownership.preserve_fields)



def test_pdf_comic_projection_uses_only_pdf_safe_metadata_and_filename_issue_identity() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "That Annihilated Place",
        ("Geoff Johns",),
        series_title="Doomsday Clock",
        sequence=SequenceNumber.parse("1"),
        run_start_year=2017,
        item_type="issue",
        publisher="DC Comics",
        release_date="2017-11-22",
        release_date_precision="day",
        language="en",
        identifiers={"comic_vine": "4000-123"},
    )

    projection = project_comic_pdf(identity)

    assert projection.destination_folder == PurePosixPath("Doomsday Clock (2017)")
    assert projection.filename == "Doomsday Clock (2017) - 001 - That Annihilated Place.pdf"
    assert projection.ownership.clear_fields == ()
    assert projection.ownership.set_fields == {
        "series": "Doomsday Clock (2017)",
        "title": "That Annihilated Place",
        "authors": ["Geoff Johns"],
        "publisher": "DC Comics",
        "date": "2017-11-22",
        "language": "en",
        "identifiers": {"comic_vine": "4000-123"},
    }
    assert "series_index" not in projection.ownership.set_fields
    assert "Series" not in projection.ownership.set_fields
    assert "Number" not in projection.ownership.set_fields



def test_standalone_collected_edition_does_not_invent_sequence_number() -> None:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "Supergirl: Woman of Tomorrow",
        ("Tom King",),
        series_title="Supergirl: Woman of Tomorrow",
        sequence=None,
        item_type="collected-edition",
        publisher="DC Comics",
    )

    assert identity.planning_blocks() == ()
    projection = project_comic(identity)

    assert "Number" not in projection.ownership.set_fields
    assert "Number" in projection.ownership.clear_fields
    assert projection.metadata["Number"] == ""
    assert " -  - " not in projection.filename
    assert projection.filename.endswith(".cbz")

def test_work_only_book_preserves_all_unresolved_edition_metadata() -> None:
    identity = work_only_identity(
        title="The Left Hand of Darkness", creators=("Ursula K. Le Guin",)
    )
    projection = project_book(identity)
    assert projection.ownership.set_fields == {
        "title": identity.title,
        "authors": ["Ursula K. Le Guin"],
    }
    assert set(projection.ownership.preserve_fields) >= {
        "publisher",
        "date",
        "language",
        "identifiers",
    }
    assert not projection.ownership.unresolved_fields


def test_book_default_hierarchy_is_title_or_series_oriented() -> None:
    standalone = CanonicalIdentity(MediaKind.BOOK, "Dune", ("Frank Herbert",))
    series = CanonicalIdentity(
        MediaKind.BOOK,
        "The Tombs of Atuan",
        ("Ursula K. Le Guin",),
        series_title="Earthsea",
        sequence=SequenceNumber.parse("2"),
    )
    assert project_book(standalone).destination == PurePosixPath("Dune/Dune.epub")
    assert project_book(series).destination == PurePosixPath(
        "Earthsea/Earthsea - 002 - The Tombs of Atuan.epub"
    )


def test_naming_policy_controls_padding_templates_and_optional_cosmetic_title() -> None:
    policy = NamingPolicy(
        comic_folder="{series} ({format})",
        comic_file="{number} - {title}",
        integer_padding=5,
        comic_specials_subfolder=False,
    )
    numbered = CanonicalIdentity(
        MediaKind.COMIC,
        "",
        (),
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
        run_start_year=1986,
        item_type="issue",
    )
    symbolic = CanonicalIdentity(
        MediaKind.COMIC,
        "",
        (),
        series_title="Watchmen",
        sequence=SequenceNumber.parse("TPB1"),
        run_start_year=1986,
        item_type="trade",
    )
    assert project_comic(numbered, naming=policy).filename == "00001.cbz"
    projected = project_comic(symbolic, naming=policy)
    assert projected.filename == "TPB1.cbz"
    assert projected.destination_folder == PurePosixPath("Watchmen (1986) (Trade Paperback)")


def test_unresolved_comic_blocks_with_precise_reasons() -> None:
    identity = CanonicalIdentity(MediaKind.COMIC, "Unknown", ())
    assert identity.planning_blocks() == (
        "comic run/series identity is unresolved",
        "comic item type is unresolved",
    )


def test_ownership_categories_and_casefolded_destinations_cannot_overlap() -> None:
    with pytest.raises(ValueError, match="overlap"):
        OwnershipManifest(set_fields={"title": "A"}, preserve_fields=("title",))
    paths = [PurePosixPath("Author/Book.epub"), PurePosixPath("author/book.EPUB")]
    assert detect_collisions(paths)
