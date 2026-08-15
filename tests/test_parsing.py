from __future__ import annotations

from pathlib import Path

import pytest

from kavita_ingest.domain import (
    Evidence,
    InspectionResult,
    InspectionStatus,
    MediaKind,
    SourceFormat,
)
from kavita_ingest.parsing import classify, tokenize_filename

EMPTY = InspectionResult(InspectionStatus.OK, SourceFormat.UNKNOWN)


@pytest.mark.parametrize(
    ("path", "format_", "kind", "subtype", "series", "sequence", "creator"),
    [
        (
            "Books/Crime and Punishment.epub",
            SourceFormat.EPUB,
            MediaKind.BOOK,
            "standalone-book",
            None,
            None,
            None,
        ),
        (
            "Books/2D Game Development_ From Zero to Hero.pdf",
            SourceFormat.PDF,
            MediaKind.BOOK,
            "standalone-book",
            None,
            None,
            None,
        ),
        (
            "Books/The Odyssey by Homer.epub",
            SourceFormat.EPUB,
            MediaKind.BOOK,
            "standalone-book",
            None,
            None,
            "Homer",
        ),
        (
            "Books/CLI Handbook Flavio Copes.epub",
            SourceFormat.EPUB,
            MediaKind.BOOK,
            "standalone-book",
            None,
            None,
            "Flavio Copes",
        ),
        (
            "Books/The Official Raspberry Pi Handbook 2023.pdf",
            SourceFormat.PDF,
            MediaKind.BOOK,
            "standalone-book",
            None,
            None,
            None,
        ),
        (
            "Comics/Absolute Batman 014 (2026) (Digital) (Shan-Empire).cbz",
            SourceFormat.CBZ,
            MediaKind.COMIC,
            "issue",
            "Absolute Batman",
            "14",
            None,
        ),
        (
            "Comics/Absolute Martian Manhunter 001 (Martianvision Activated) "
            "(2025) (2 covers) (c2c) (Ignatz-DCP).cbz",
            SourceFormat.CBZ,
            MediaKind.COMIC,
            "issue",
            "Absolute Martian Manhunter",
            "1",
            None,
        ),
        (
            "Comics/Animal Man by Grant Morrison Book 01 (2020).cbr",
            SourceFormat.CBR,
            MediaKind.COMIC,
            "collected-edition",
            "Animal Man",
            "1",
            "Grant Morrison",
        ),
        (
            "Comics/New X-Men by Grant Morrison Ultimate Collection Book 1 "
            "(2019) (Digital) (Asgard-Empire).cbz",
            SourceFormat.CBZ,
            MediaKind.COMIC,
            "collected-edition",
            "New X-Men",
            "1",
            "Grant Morrison",
        ),
        (
            "Comics/What If - V1 - 024 - Spider-Man Had Rescued Gwen Stacy.pdf",
            SourceFormat.PDF,
            MediaKind.COMIC,
            "issue",
            "What If",
            "24",
            None,
        ),
        (
            "Comics/What If - V1 - 034 -The Watcher were a stand up comedian.pdf",
            SourceFormat.PDF,
            MediaKind.COMIC,
            "issue",
            "What If",
            "34",
            None,
        ),
        (
            "Comics/Watchmen/Watchmen #1.pdf",
            SourceFormat.PDF,
            MediaKind.COMIC,
            "issue",
            "Watchmen",
            "1",
            None,
        ),
        (
            "Comics/Watchmen Doomsday Clock/Doomsday Clock #12.pdf",
            SourceFormat.PDF,
            MediaKind.COMIC,
            "issue",
            "Doomsday Clock",
            "12",
            None,
        ),
        (
            "Comics/Superman - The Kryptonite Spectrum (2026) (digital) (Son of Ultron-Empire).cbr",
            SourceFormat.CBR,
            MediaKind.COMIC,
            "one-shot",
            "Superman - The Kryptonite Spectrum",
            None,
            None,
        ),
    ],
)
def test_real_world_filename_regressions(
    path: str,
    format_: SourceFormat,
    kind: MediaKind,
    subtype: str,
    series: str | None,
    sequence: str | None,
    creator: str | None,
) -> None:
    result = classify(Path(path), format_, EMPTY)
    hypothesis = result.hypotheses[0]
    assert result.kind is kind
    assert hypothesis.subtype == subtype
    assert hypothesis.series == series
    assert (hypothesis.sequence.normalized if hypothesis.sequence else None) == sequence
    assert (hypothesis.creators[0] if hypothesis.creators else None) == creator


def test_release_noise_is_preserved_as_provenance_but_not_identity() -> None:
    path = Path("Absolute Batman 014 (2026) (Digital) (Shan-Empire).cbz")
    result = classify(path, SourceFormat.CBZ, EMPTY)
    noise = [item for item in result.hypotheses[0].evidence if item.is_noise]
    assert {item.raw for item in noise} == {"Digital", "Shan-Empire"}
    assert result.hypotheses[0].series == "Absolute Batman"


def test_generic_pdf_retains_competing_hypotheses() -> None:
    result = classify(Path("Mystery.pdf"), SourceFormat.PDF, EMPTY)
    assert result.ambiguous is True
    assert {item.kind for item in result.hypotheses} == {MediaKind.BOOK, MediaKind.COMIC}


def test_tokenizer_retains_raw_spans() -> None:
    tokens = tokenize_filename(Path("Series 001 (Digital) (2025).cbz"))
    assert any(token.raw == "001" and token.kind == "sequence" for token in tokens)
    assert any(token.raw == "Digital" and token.is_noise for token in tokens)
    assert any(token.raw == "2025" and token.kind == "year" for token in tokens)


def test_structured_collection_filename_outweighs_overloaded_embedded_series() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBR,
        metadata={
            "comicinfo": {
                "Series": "Animal Man by Grant Morrison Book One",
                "Number": "1",
            }
        },
        evidence=(
            Evidence(
                "series",
                "Animal Man by Grant Morrison Book One",
                "Animal Man by Grant Morrison Book One",
                "comicinfo",
                0.99,
            ),
        ),
    )
    result = classify(
        Path("Animal Man by Grant Morrison Book 01 (2020).cbr"),
        SourceFormat.CBR,
        inspection,
    )
    assert result.hypotheses[0].subtype == "collected-edition"
    assert result.hypotheses[0].series == "Animal Man"
    assert any(
        "separates series, creator, and book index" in reason
        for reason in result.hypotheses[0].reasons
    )
    assert any(
        item.source == "comicinfo" and item.raw == "Animal Man by Grant Morrison Book One"
        for item in result.hypotheses[0].evidence
    )


def test_long_unnumbered_comic_archive_is_not_confidently_called_a_one_shot() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBR,
        metadata={"page_count": 181, "entry_count": 181},
    )

    result = classify(
        Path("Superman - The Kryptonite Spectrum (2026) (digital).cbr"),
        SourceFormat.CBR,
        inspection,
    )

    assert result.ambiguous is True
    assert result.subtype == "collected-edition"
    assert [item.subtype for item in result.hypotheses] == [
        "collected-edition",
        "one-shot",
    ]


def test_long_unnumbered_collection_extracts_safe_filename_creator_credit() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBR,
        metadata={"page_count": 180, "entry_count": 180},
    )

    result = classify(
        Path("Hawkeye by Matt Fraction and David Aja (2015) (Digital).cbr"),
        SourceFormat.CBR,
        inspection,
    )

    hypothesis = result.hypotheses[0]
    assert hypothesis.subtype == "collected-edition"
    assert hypothesis.series == "Hawkeye"
    assert hypothesis.title == "Hawkeye"
    assert hypothesis.creators == ("Matt Fraction", "David Aja")


def test_long_unnumbered_collection_does_not_split_title_by_gaslight() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBR,
        metadata={"page_count": 180, "entry_count": 180},
    )

    result = classify(Path("Batman by Gaslight (2019).cbr"), SourceFormat.CBR, inspection)

    hypothesis = result.hypotheses[0]
    assert hypothesis.subtype == "collected-edition"
    assert hypothesis.series == "Batman by Gaslight"
    assert hypothesis.creators == ()

@pytest.mark.parametrize(
    (
        "path",
        "subtype",
        "series",
        "title",
        "sequence",
        "year",
        "edition_qualifiers",
    ),
    [
        (
            "Bone - The Complete Cartoon Epic in One Volume (2004) GetComics.INFO.cbr",
            "collected-edition",
            "Bone",
            "The Complete Cartoon Epic in One Volume",
            None,
            2004,
            ("The Complete Cartoon Epic in One Volume",),
        ),
        (
            "Saga v01 (2012).cbr",
            "collected-edition",
            "Saga",
            "Volume 1",
            "1",
            2012,
            (),
        ),
        (
            "Saga v02 (2013) (Digital-TPB) (Zone-Empire).cbr",
            "collected-edition",
            "Saga",
            "Volume 2",
            "2",
            2013,
            (),
        ),
        (
            "Saga v05 (2015) GetComics.INFO.cbr",
            "collected-edition",
            "Saga",
            "Volume 5",
            "5",
            2015,
            (),
        ),
        (
            "Spider-Man - Life Story (2021, 2nd edition) (Digital) (Zone-Empire).cbr",
            "collected-edition",
            "Spider-Man - Life Story",
            "2nd edition",
            None,
            2021,
            ("2nd edition",),
        ),
        (
            "All-Star Superman (2018, DC Black Label Edition) (digital) "
            "(Son of Ultron-Empire).cbr",
            "collected-edition",
            "All-Star Superman",
            "DC Black Label Edition",
            None,
            2018,
            ("DC Black Label Edition",),
        ),
    ],
)
def test_production_collection_filename_semantics(
    path: str,
    subtype: str,
    series: str,
    title: str,
    sequence: str | None,
    year: int,
    edition_qualifiers: tuple[str, ...],
) -> None:
    result = classify(Path(path), SourceFormat.CBR, EMPTY)
    hypothesis = result.hypotheses[0]

    assert hypothesis.subtype == subtype
    assert hypothesis.series == series
    assert hypothesis.title == title
    assert (hypothesis.sequence.normalized if hypothesis.sequence else None) == sequence
    assert hypothesis.year == year
    assert hypothesis.edition_qualifiers == edition_qualifiers


def test_release_site_and_digital_tpb_are_noise_not_identity() -> None:
    path = Path("Saga v02 (2013) (Digital-TPB) (Zone-Empire) GetComics.INFO.cbr")
    result = classify(path, SourceFormat.CBR, EMPTY)
    hypothesis = result.hypotheses[0]
    noise = {item.raw for item in hypothesis.evidence if item.is_noise}

    assert {"Digital-TPB", "Zone-Empire", "GetComics.INFO"} <= noise
    assert hypothesis.series == "Saga"
    assert hypothesis.title == "Volume 2"


def test_compound_year_parenthetical_emits_structured_edition_evidence() -> None:
    tokens = tokenize_filename(
        Path("Spider-Man - Life Story (2021, 2nd edition) (Digital).cbr")
    )

    assert any(token.raw == "2021" and token.kind == "year" for token in tokens)
    assert any(
        token.raw == "2nd edition" and token.kind == "edition-qualifier"
        for token in tokens
    )


def test_embedded_creator_credit_series_alias_does_not_override_clean_issue_filename() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBR,
        metadata={
            "comicinfo": {
                "Series": "Oblivion Song By Kirkman & De Felici",
                "Number": "1",
                "Title": "Chapter One",
            }
        },
    )

    result = classify(
        Path("Oblivion Song 001 (2018) GetComics.INFO.cbr"),
        SourceFormat.CBR,
        inspection,
    )
    hypothesis = result.hypotheses[0]

    assert hypothesis.subtype == "issue"
    assert hypothesis.series == "Oblivion Song"
    assert hypothesis.title == "Chapter One"
    assert hypothesis.sequence is not None
    assert hypothesis.sequence.normalized == "1"
    assert hypothesis.year == 2018
    assert any("creator-credit alias" in reason for reason in hypothesis.reasons)


def test_genuine_by_title_is_not_reinterpreted_as_creator_alias() -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBZ,
        metadata={"comicinfo": {"Series": "Batman by Gaslight", "Number": "1"}},
    )

    result = classify(
        Path("Batman by Gaslight 001 (2019).cbz"),
        SourceFormat.CBZ,
        inspection,
    )

    assert result.hypotheses[0].series == "Batman by Gaslight"
