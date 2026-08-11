from __future__ import annotations

from pathlib import Path

from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import (
    DecisionRepository,
    DecisionType,
    accept_candidate,
    add_manual_identity,
    add_manual_override,
)
from kavita_ingest.domain import MediaKind, SequenceNumber, SourceFormat, SourceRecord
from kavita_ingest.matching import CandidateScore, Reconciliation
from kavita_ingest.projection import project_book, project_comic
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
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


def test_all_supported_manual_override_types_affect_resolved_identity(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        add_manual_identity(
            repository,
            source,
            "evidence",
            {"title": "Known", "authors": "Original Author"},
        )
        for field, value in (
            ("isbn", "978-0-140-26886-7"),
            ("translators", "Terry Translator"),
            ("run_start_year", "2024"),
            ("collection_volume", "2"),
        ):
            add_manual_override(repository, source, "evidence", field, value)
        resolved = resolve_explicit_identity(repository, source, MediaKind.BOOK)
    assert resolved.identity is not None
    assert resolved.identity.identifiers == {"isbn": "9780140268867"}
    assert resolved.identity.contributors == {"translators": ("Terry Translator",)}
    assert resolved.identity.run_start_year == 2024
    assert resolved.identity.collection_volume == 2


def test_comic_candidate_roles_survive_canonical_identity_and_projection(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    candidate = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-140001",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "The Zoo",
        creators=(
            Contributor("Scott Snyder", "writer"),
            Contributor("Nick Dragotta", "penciller"),
            Contributor("Mystery Person", "unknown:designer"),
        ),
        publisher="DC Comics",
        release_date="2025-11-26",
        release_date_precision="day",
        cover_date="2026-01",
        cover_date_precision="month",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        run_start_year=2024,
        item_type="issue",
        run_id="4050-160294",
    )
    score = CandidateScore(candidate, 99, 0.99, (), (), False, True, eligible=True)
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        accept_candidate(repository, source, score, Reconciliation(None, None, (), ()), "evidence")
        resolved = resolve_explicit_identity(repository, source, MediaKind.COMIC)
    assert resolved.identity is not None
    assert resolved.identity.creators == ("Scott Snyder",)
    assert resolved.identity.contributors == {
        "writers": ("Scott Snyder",),
        "pencillers": ("Nick Dragotta",),
        "unknown:designer": ("Mystery Person",),
    }
    projection = project_comic(resolved.identity)
    assert projection.metadata["Writer"] == "Scott Snyder"
    assert projection.metadata["Publisher"] == "DC Comics"
    assert (
        projection.metadata["Year"],
        projection.metadata["Month"],
        projection.metadata["Day"],
    ) == (
        2025,
        11,
        26,
    )
    assert resolved.identity.cover_date == "2026-01"


def test_historical_complete_book_work_decision_still_resolves_work_only(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = _source()
    candidate = NormalizedCandidate(
        ProviderName.OPEN_LIBRARY,
        "works/OL123W",
        RecordType.BOOK_WORK,
        MediaKind.BOOK,
        "Crime and Punishment",
        creators=(Contributor("Fyodor Dostoevsky", "author"),),
        identifiers=(Identifier("isbn", "9780140449136"),),
        publisher="Aggregate Publisher",
        publication_date="1866",
        language="eng",
        work_id="works/OL123W",
    )
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        repository.add(
            source,
            DecisionType.ACCEPTED,
            "evidence",
            candidate_key=candidate.key,
            candidate_data_hash=candidate.data_hash(),
            payload={"candidate": candidate.to_dict(), "explicit": True},
        )
        resolved = resolve_explicit_identity(repository, source, MediaKind.BOOK)
    assert resolved.identity is not None
    assert resolved.identity.resolution.value == "work_only"
    assert resolved.identity.publisher is None
    assert resolved.identity.publication_date is None
    assert resolved.identity.language is None
    assert resolved.identity.identifiers == {}
    projection = project_book(resolved.identity)
    assert set(projection.ownership.preserve_fields) >= {
        "publisher",
        "date",
        "language",
        "identifiers",
    }
