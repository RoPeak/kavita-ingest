from __future__ import annotations

from kavita_ingest.config import MatchingSettings
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.matching import (
    ComparisonKind,
    LocalIdentity,
    reconcile,
    score_candidates,
)
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
)

SETTINGS = MatchingSettings()


def _book(
    provider_id: str = "edition-1",
    *,
    title: str = "The Odyssey",
    isbn: str = "9780140268867",
    record_type: RecordType = RecordType.BOOK_EDITION,
) -> NormalizedCandidate:
    return NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        provider_id,
        record_type,
        MediaKind.BOOK,
        title,
        creators=(Contributor("Homer", "author"),),
        identifiers=(Identifier("isbn", isbn),),
        publisher="Fixture Classics",
        publication_date="2020-05-01",
    )


def test_exact_identifier_is_decisive_and_explanation_is_detailed() -> None:
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "The Odyssey",
        ("Homer",),
        (Identifier("isbn", "9780140268867"),),
    )
    score = score_candidates(local, [_book()], SETTINGS)[0]
    assert score.score == 100
    assert score.eligible is True
    assert score.runner_up_margin == 100
    assert any(
        item.field == "identifier" and item.kind is ComparisonKind.EXACT
        for item in score.comparisons
    )
    assert any("exact isbn identifier" in line for line in score.explanation())
    assert reconcile(local, score).edition_state == "accepted"
    assert reconcile(local, score).fields[0].provenance == ("google_books", "edition-1")


def test_conflicting_exact_identifier_is_a_hard_contradiction() -> None:
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "The Odyssey",
        ("Homer",),
        (Identifier("isbn", "9780140268867"),),
    )
    score = score_candidates(local, [_book(isbn="9780140449112")], SETTINGS)[0]
    assert score.hard_contradiction is True
    assert score.eligible is False
    assert "conflicting exact isbn identifiers" in score.contradictions


def test_title_author_fuzzy_match_can_accept_work_but_not_edition() -> None:
    local = LocalIdentity(
        MediaKind.BOOK,
        "standalone-book",
        0.98,
        "Crime & Punishment",
        ("Fyodor Dostoevsky",),
    )
    work = NormalizedCandidate(
        ProviderName.OPEN_LIBRARY,
        "OL123W",
        RecordType.BOOK_WORK,
        MediaKind.BOOK,
        "Crime and Punishment",
        creators=(Contributor("Fyodor Dostoevsky", "author"),),
    )
    score = score_candidates(local, [work], SETTINGS)[0]
    resolved = reconcile(local, score)
    assert score.score >= 80
    assert resolved.work_state == "accepted"
    assert resolved.edition_state == "unresolved"
    assert "no exact edition identifier" in resolved.reason[0]


def test_comic_series_sequence_and_run_year_score_independently() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "The Zoo",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("014"),
        year=2026,
    )
    issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-1",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "The Zoo",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        run_start_year=2024,
        publication_date="2026-01-15",
    )
    score = score_candidates(local, [issue], SETTINGS)[0]
    assert score.eligible is True
    assert any(item.field == "sequence" and item.score_delta == 30 for item in score.comparisons)
    assert issue.run_start_year == 2024


def test_collected_edition_is_blocked_from_issue_candidate_path() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "Animal Man Book 1",
        creators=("Grant Morrison",),
        sequence=SequenceNumber.parse("1"),
        series_title="Animal Man",
    )
    issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-1",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Animal Man",
        series_title="Animal Man",
        sequence=SequenceNumber.parse("1"),
    )
    score = score_candidates(local, [issue], SETTINGS)[0]
    assert score.hard_contradiction
    assert "collected edition cannot resolve to a regular issue" in score.contradictions


def test_runner_up_margin_prevents_automatic_eligibility() -> None:
    local = LocalIdentity(MediaKind.BOOK, "standalone-book", 0.98, "The Odyssey", ("Homer",))
    scores = score_candidates(
        local,
        [_book("one", isbn=""), _book("two", isbn="")],
        SETTINGS,
    )
    assert scores[0].runner_up_margin < SETTINGS.eligible_margin
    assert scores[0].eligible is False
