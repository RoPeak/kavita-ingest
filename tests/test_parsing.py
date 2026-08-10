from __future__ import annotations

from pathlib import Path

import pytest

from kavita_ingest.domain import InspectionResult, InspectionStatus, MediaKind, SourceFormat
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
