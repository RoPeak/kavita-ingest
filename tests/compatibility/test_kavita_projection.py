from __future__ import annotations

import pytest

from compatibility.helpers.kavita_projection import CanonicalComic, SequenceNumber, project


def test_same_named_runs_are_disambiguated_in_projected_series_not_canonical_identity() -> None:
    first = CanonicalComic("Absolute Batman", 2024, "issue", SequenceNumber("1"))
    second = CanonicalComic("Absolute Batman", 2031, "issue", SequenceNumber("1"))
    first_projection = project(first)
    second_projection = project(second)
    assert first.series_title == second.series_title == "Absolute Batman"
    assert first_projection.series == "Absolute Batman (2024)"
    assert second_projection.series == "Absolute Batman (2031)"
    assert first_projection.volume is None
    assert second_projection.volume is None


@pytest.mark.parametrize(
    ("raw", "metadata", "filename"),
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
def test_sequence_projection_preserves_meaning(raw: str, metadata: str, filename: str) -> None:
    projection = project(CanonicalComic("Series", 2024, "issue", SequenceNumber(raw)))
    assert projection.number == metadata
    assert f"#{filename}" in projection.filename


@pytest.mark.parametrize(
    ("comic", "number", "volume", "format_value", "filename_fragment"),
    [
        (CanonicalComic("Series", 2024, "annual", SequenceNumber("2")), "2", None, "Annual", "Annual 02"),
        (CanonicalComic("Series", 2024, "special"), "1", None, "Special", "SP01"),
        (CanonicalComic("Series", 2024, "one_shot"), "1", None, "One-Shot", "SP01"),
        (
            CanonicalComic("Series", 2024, "trade", collection_sequence=SequenceNumber("1")),
            None,
            1,
            "TPB",
            "v01",
        ),
        (
            CanonicalComic("Series", 2024, "trade", collection_sequence=SequenceNumber("TPB1")),
            "TPB1",
            None,
            "TPB",
            "SPTPB1",
        ),
        (
            CanonicalComic("Series", 2024, "omnibus", collection_sequence=SequenceNumber("2")),
            None,
            2,
            "Omnibus",
            "v02",
        ),
        (
            CanonicalComic("Standalone Graphic Novel", None, "graphic_novel"),
            None,
            None,
            "Graphic Novel",
            "SP01",
        ),
    ],
)
def test_item_type_projection(
    comic: CanonicalComic,
    number: str | None,
    volume: int | None,
    format_value: str,
    filename_fragment: str,
) -> None:
    projection = project(comic)
    assert projection.number == number
    assert projection.volume == volume
    assert projection.format == format_value
    assert filename_fragment in projection.filename


def test_issue_title_suffix_is_optional() -> None:
    without_title = project(CanonicalComic("Series", 2024, "issue", SequenceNumber("1")))
    with_title = project(
        CanonicalComic("Series", 2024, "issue", SequenceNumber("1"), title="A Title")
    )
    assert without_title.filename == "Series (2024) #001.cbz"
    assert with_title.filename == "Series (2024) #001 - A Title.cbz"
