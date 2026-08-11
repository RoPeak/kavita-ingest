from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from kavita_ingest.candidates import CandidateSession, generate_candidates
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
from kavita_ingest.domain import MediaKind, SequenceNumber, SourceFormat, SourceRecord
from kavita_ingest.matching import LocalIdentity, reconcile, score_candidates
from kavita_ingest.providers.base import ProviderStatus
from kavita_ingest.providers.comic_vine import ComicVineProvider
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


class RunReuseClient:
    def __init__(self, *, same_year: bool = False) -> None:
        self.operations: list[str] = []
        self.same_year = same_year

    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Callable[[object], list[NormalizedCandidate]],
    ) -> list[NormalizedCandidate]:
        del url, secret_params, bucket
        self.operations.append(operation)
        if operation == "search-runs":
            raw = json.loads(
                Path("tests/fixtures/providers/comic_vine_runs.json").read_text(encoding="utf-8")
            )
            if self.same_year:
                raw["results"][1]["start_year"] = "2024"
        else:
            raw = json.loads(
                Path("tests/fixtures/providers/comic_vine.json").read_text(encoding="utf-8")
            )
            if "volume:167340" in public_params["filter"]:
                raw["results"] = []
                return normalize(raw)
            number = public_params["filter"].rsplit(":", 1)[-1]
            raw["results"][0]["issue_number"] = number
        return normalize(raw)


class TitleDisambiguationClient:
    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Callable[[object], list[NormalizedCandidate]],
    ) -> list[NormalizedCandidate]:
        del url, secret_params, bucket
        if operation == "search":
            return normalize({"results": []})
        if operation == "search-runs":
            return normalize(
                {
                    "results": [
                        _comic_vine_record("volume", 2918, "What If?", start_year=1977),
                        _comic_vine_record("volume", 4249, "What If...?", start_year=1989),
                    ]
                }
            )
        volume = int(public_params["filter"].split(",", 1)[0].split(":", 1)[1])
        number = public_params["filter"].rsplit(":", 1)[-1]
        title = (
            "What if Spider-Man joined the Fantastic Four?"
            if volume == 2918 and number == "1"
            else "What If the Avengers Lost the Evolutionary War?"
        )
        return normalize(
            {
                "results": [
                    {
                        **_comic_vine_record("issue", volume * 100 + int(number), title),
                        "issue_number": number,
                        "volume": {
                            "id": volume,
                            "name": "What If?",
                            "api_detail_url": (
                                f"https://comicvine.gamespot.com/api/volume/4050-{volume}/"
                            ),
                        },
                    }
                ]
            }
        )


def _comic_vine_record(
    resource: str, identifier: int, name: str, *, start_year: int | None = None
) -> dict[str, object]:
    prefix = "4050" if resource == "volume" else "4000"
    return {
        "id": identifier,
        "resource_type": resource,
        "api_detail_url": (
            f"https://comicvine.gamespot.com/api/{resource}/{prefix}-{identifier}/"
        ),
        "name": name,
        "start_year": start_year,
        "publisher": {"name": "Marvel"},
    }

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


def test_comic_run_is_resolved_once_and_reused_without_implicit_approval() -> None:
    client = RunReuseClient()
    provider = ComicVineProvider(client, "secret")  # type: ignore[arg-type]
    first = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("1"),
        year=2024,
    )
    later = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        year=2026,
    )
    session = CandidateSession.from_local_identities([first, later])

    first_result = generate_candidates(first, (provider,), session)
    later_result = generate_candidates(later, (provider,), session)

    assert client.operations == ["search-runs", "issues-in-run", "issues-in-run"]
    assert first_result.candidates[0].run_id == "4050-160294"
    assert later_result.candidates[0].sequence == SequenceNumber.parse("14")
    assert "comic_vine:run-reused" in later_result.queries
    assert session.metrics()["repeated_run_queries_avoided"] == 1
    assert session.metrics()["run_disambiguation_queries"] == 0


def test_comic_run_uses_issue_title_only_when_it_disambiguates() -> None:
    provider = ComicVineProvider(TitleDisambiguationClient(), "secret")  # type: ignore[arg-type]
    first = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.92,
        "Spider-Man Joined the Fantastic Four",
        series_title="What If",
        sequence=SequenceNumber.parse("1"),
    )
    last = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.92,
        "Loki Had Found the Hammer of Thor",
        series_title="What If",
        sequence=SequenceNumber.parse("47"),
    )
    session = CandidateSession.from_local_identities([first, last])

    result = generate_candidates(first, (provider,), session)

    assert result.candidates[0].run_id == "4050-2918"
    assert session.resolved_runs["what if"].provider_id == "4050-2918"  # type: ignore[union-attr]


def test_highest_local_issue_disambiguates_same_year_collection_run() -> None:
    client = RunReuseClient(same_year=True)
    provider = ComicVineProvider(client, "secret")  # type: ignore[arg-type]
    first = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("1"),
        year=2024,
    )
    later = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        year=2026,
    )
    session = CandidateSession.from_local_identities([first, later])

    generate_candidates(first, (provider,), session)

    resolved = session.resolved_runs["absolute batman"]
    assert resolved is not None and resolved.provider_id == "4050-160294"
    assert session.metrics()["run_disambiguation_queries"] == 2


def test_run_disambiguation_budget_preserves_ambiguity_instead_of_overquerying() -> None:
    provider = ComicVineProvider(TitleDisambiguationClient(), "secret")  # type: ignore[arg-type]
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.92,
        "Spider-Man Joined the Fantastic Four",
        series_title="What If",
        sequence=SequenceNumber.parse("1"),
    )
    session = CandidateSession.from_local_identities([local])
    session.max_disambiguation_queries = 1

    result = generate_candidates(local, (provider,), session)

    assert not result.candidates
    assert session.resolved_runs["what if"] is None
    assert session.metrics()["run_disambiguation_queries"] == 1
    assert session.metrics()["disambiguation_budget_exhausted"] == 1


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
