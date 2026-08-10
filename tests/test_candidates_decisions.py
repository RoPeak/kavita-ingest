from __future__ import annotations

from pathlib import Path

import pytest

from kavita_ingest.candidates import generate_candidates
from kavita_ingest.config import MatchingSettings
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import (
    DecisionRepository,
    DecisionType,
    accept_candidate,
    add_manual_identity,
    add_manual_override,
    batch_accept,
    clear_manual_override,
    validate_manual_override,
)
from kavita_ingest.domain import MediaKind, SourceFormat, SourceRecord
from kavita_ingest.matching import LocalIdentity, reconcile, score_candidates
from kavita_ingest.providers.base import ProviderStatus
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)


class FakeProvider:
    def __init__(
        self,
        name: ProviderName,
        candidates: list[NormalizedCandidate],
        *,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.candidates = candidates
        self.enabled = enabled
        self.operations: list[str] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            self.name,
            self.enabled,
            False,
            True,
            "fixture" if self.enabled else "missing fixture credential",
            ("search",),
        )

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        self.operations.append(f"search:{query.item_type}:{query.relaxed}")
        return self.candidates

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        self.operations.append("fetch")
        return self.candidates

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        self.operations.append(f"identifier:{identifier.scheme}")
        return self.candidates


def _edition() -> NormalizedCandidate:
    return NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "edition",
        RecordType.BOOK_EDITION,
        MediaKind.BOOK,
        "The Odyssey",
        creators=(Contributor("Homer", "author"),),
        identifiers=(Identifier("isbn", "9780140268867"),),
    )


def _source(path: str = "/incoming/book.epub") -> SourceRecord:
    return SourceRecord(Path(path), 100, 1, "a" * 64, SourceFormat.EPUB, "zip-epub")


def _repository(tmp_path: Path) -> tuple[DecisionRepository, object]:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    connection = connect(database)
    return DecisionRepository(connection), connection


def test_query_strategy_uses_identifier_before_structured_search() -> None:
    provider = FakeProvider(ProviderName.GOOGLE_BOOKS, [_edition()])
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "The Odyssey",
        ("Homer",),
        (Identifier("isbn", "9780140268867"),),
    )
    result = generate_candidates(local, (provider,))
    assert provider.operations == ["identifier:isbn"]
    assert result.queries == ("google_books:identifier:isbn",)


def test_collected_edition_query_retains_collection_semantics() -> None:
    provider = FakeProvider(ProviderName.COMIC_VINE, [])
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "Book 1",
        series_title="Animal Man",
    )
    generate_candidates(local, (provider,))
    assert provider.operations[0].startswith("search:collected-edition")


def test_unavailable_provider_does_not_block_other_or_cached_workflows() -> None:
    disabled = FakeProvider(ProviderName.COMIC_VINE, [], enabled=False)
    local = LocalIdentity(MediaKind.COMIC, "issue", 0.98, "Watchmen", series_title="Watchmen")
    result = generate_candidates(local, (disabled,))
    assert not result.candidates
    assert "missing fixture credential" in result.unavailable[0]


def test_score_alone_never_creates_decision_and_acceptance_is_explicit(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "The Odyssey",
        ("Homer",),
        (Identifier("isbn", "9780140268867"),),
    )
    score = score_candidates(local, [_edition()], MatchingSettings())[0]
    assert score.eligible
    assert connection.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    decision = accept_candidate(
        repository, _source(), score, reconcile(local, score), local.evidence_hash()
    )
    assert decision.decision_type is DecisionType.ACCEPTED
    assert decision.payload["explicit"] is True
    connection.close()


def test_decisions_follow_content_not_path_and_preserve_history(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    source = _source()
    moved = _source("/library/moved.epub")
    first = repository.add(source, DecisionType.UNRESOLVED, "evidence-1")
    second = repository.add(moved, DecisionType.SKIPPED, "evidence-1")
    assert second.supersedes_id == first.id
    assert [item.decision_type for item in repository.history(moved)] == [
        DecisionType.UNRESOLVED,
        DecisionType.SKIPPED,
    ]
    connection.close()


def test_rejection_stays_suppressed_until_evidence_or_candidate_changes(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    source = _source()
    candidate = _edition()
    repository.add(
        source,
        DecisionType.REJECTED,
        "evidence-1",
        candidate_key=candidate.key,
        candidate_data_hash=candidate.data_hash(),
    )
    assert repository.rejection_suppresses(
        source, candidate.key, "evidence-1", candidate.data_hash()
    )
    assert not repository.rejection_suppresses(
        source, candidate.key, "evidence-2", candidate.data_hash()
    )
    assert not repository.rejection_suppresses(
        source, candidate.key, "evidence-1", "changed-candidate"
    )
    repository.clear_rejection(source, candidate.key, "evidence-1")
    assert not repository.rejection_suppresses(
        source, candidate.key, "evidence-1", candidate.data_hash()
    )
    connection.close()


def test_manual_overrides_are_typed_sticky_and_cumulative(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    source = _source()
    add_manual_override(repository, source, "evidence", "isbn", "978-0-140-26886-7")
    add_manual_override(repository, source, "evidence", "language", "en-GB")
    assert repository.manual_overrides(source) == {
        "isbn": "9780140268867",
        "language": "en-GB",
    }
    assert repository.history(source)[-1].payload["provenance"] == "user"
    assert validate_manual_override("sequence", "001") == "1"
    with pytest.raises(ValueError, match="ISBN"):
        validate_manual_override("isbn", "9780140268868")
    clear_manual_override(repository, source, "evidence", "isbn")
    assert repository.manual_overrides(source) == {"language": "en-GB"}
    connection.close()


def test_manual_canonical_identity_requires_explicit_validated_fields(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    decision = add_manual_identity(
        repository,
        _source(),
        "evidence",
        {"title": "A Manually Identified Book", "language": "en"},
    )
    assert decision.decision_type is DecisionType.MANUAL_IDENTITY
    assert decision.payload["provenance"] == "user"
    with pytest.raises(ValueError, match="requires a title"):
        add_manual_identity(repository, _source(), "evidence", {"language": "en"})
    connection.close()


def test_batch_accept_requires_exact_count_and_excludes_unresolved_editions(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "The Odyssey",
        ("Homer",),
        (Identifier("isbn", "9780140268867"),),
    )
    edition_score = score_candidates(local, [_edition()], MatchingSettings())[0]
    edition_item = (
        _source(),
        edition_score,
        reconcile(local, edition_score),
        local.evidence_hash(),
    )
    work_candidate = NormalizedCandidate(
        ProviderName.OPEN_LIBRARY,
        "work",
        RecordType.BOOK_WORK,
        MediaKind.BOOK,
        "The Odyssey",
        creators=(Contributor("Homer", "author"),),
        identifiers=(Identifier("isbn", "9780140268867"),),
    )
    work_score = score_candidates(local, [work_candidate], MatchingSettings())[0]
    work_item = (
        _source("/incoming/work.epub"),
        work_score,
        reconcile(local, work_score),
        local.evidence_hash(),
    )
    with pytest.raises(ValueError, match="exactly 1"):
        batch_accept(repository, [edition_item, work_item], confirmed_count=2)
    accepted = batch_accept(repository, [edition_item, work_item], confirmed_count=1)
    assert len(accepted) == 1
    assert accepted[0].batch_id
    connection.close()
