from __future__ import annotations

from pathlib import Path

from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import (
    DecisionRepository,
    DecisionType,
    accept_candidate,
    add_manual_identity,
)
from kavita_ingest.domain import MediaKind, SourceFormat, SourceRecord
from kavita_ingest.matching import CandidateScore, Reconciliation
from kavita_ingest.providers.models import (
    Contributor,
    NormalizedCandidate,
    ProviderName,
    RecordType,
)
from kavita_ingest.resolution import resolve_explicit_identity


def _source() -> SourceRecord:
    return SourceRecord(Path("/incoming/book.epub"), 10, 20, "a" * 64, SourceFormat.EPUB, "epub")


def test_work_only_decision_resolves_work_but_does_not_invent_edition(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    candidate = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "edition-1",
        RecordType.BOOK_EDITION,
        MediaKind.BOOK,
        "Earthsea",
        creators=(Contributor("Ursula K. Le Guin", "author"),),
        publisher="Edition Press",
        publication_date="2020-01-02",
        language="en",
    )
    score = CandidateScore(candidate, 99, 0.99, (), (), False, True, eligible=True)
    reconciliation = Reconciliation("accepted", "unresolved", (), ())
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        accept_candidate(repository, source, score, reconciliation, "evidence", work_only=True)
        resolved = resolve_explicit_identity(repository, source, MediaKind.BOOK)
    assert resolved.eligible
    assert resolved.identity is not None
    assert resolved.identity.title == "Earthsea"
    assert resolved.identity.publisher is None
    assert resolved.identity.publication_date is None
    assert resolved.identity.language is None


def test_manual_canonical_comic_needs_no_provider_id(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        add_manual_identity(
            repository,
            source,
            "evidence",
            {"series_title": "Local Graphic Novel", "item_type": "graphic-novel"},
        )
        resolved = resolve_explicit_identity(repository, source, MediaKind.COMIC)
    assert resolved.eligible
    assert resolved.identity is not None and not resolved.identity.provider_identity


def test_latest_unresolved_decision_blocks_an_older_acceptance(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        add_manual_identity(repository, source, "evidence", {"title": "Known"})
        repository.add(source, DecisionType.UNRESOLVED, "evidence", payload={"explicit": True})
        resolved = resolve_explicit_identity(repository, source, MediaKind.BOOK)
    assert not resolved.eligible
    assert resolved.blocks == ("latest explicit decision is unresolved",)
