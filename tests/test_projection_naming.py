from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel, work_only_identity
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.naming import NamingPolicy, detect_collisions
from kavita_ingest.projection import OwnershipManifest, project_book, project_comic


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
    assert projected.destination_folder == PurePosixPath(
        "Watchmen (1986) (Trade Paperback)"
    )


def test_unresolved_comic_blocks_with_precise_reasons() -> None:
    identity = CanonicalIdentity(MediaKind.COMIC, "Unknown", ())
    assert identity.planning_blocks() == (
        "comic run/series identity is unresolved",
        "comic item type is unresolved",
        "comic issue/collection sequence is unresolved",
    )


def test_ownership_categories_and_casefolded_destinations_cannot_overlap() -> None:
    with pytest.raises(ValueError, match="overlap"):
        OwnershipManifest(set_fields={"title": "A"}, preserve_fields=("title",))
    paths = [PurePosixPath("Author/Book.epub"), PurePosixPath("author/book.EPUB")]
    assert detect_collisions(paths)
