from __future__ import annotations

from kavita_ingest.config import MatchingSettings
from kavita_ingest.domain import Classification, MediaKind, ParseHypothesis, SequenceNumber
from kavita_ingest.matching import (
    ComparisonKind,
    LocalIdentity,
    local_identity,
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


def test_comic_series_is_not_promoted_to_missing_issue_title() -> None:
    classification = Classification(
        MediaKind.COMIC,
        "issue",
        0.98,
        False,
        (
            ParseHypothesis(
                MediaKind.COMIC,
                "issue",
                0.98,
                series="Watchmen",
                sequence=SequenceNumber.parse("1"),
            ),
        ),
    )

    local = local_identity(classification, {})

    assert local.title == ""
    assert local.series_title == "Watchmen"


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
        cover_date="2026-01",
        cover_date_precision="month",
        release_date="2025-11-26",
        release_date_precision="day",
    )
    score = score_candidates(local, [issue], SETTINGS)[0]
    assert score.eligible is True
    assert any(item.field == "sequence" and item.score_delta == 30 for item in score.comparisons)
    assert any(
        item.field == "issue_title" and item.kind is ComparisonKind.EXACT
        for item in score.comparisons
    )
    assert issue.run_start_year == 2024


def test_issue_candidate_without_run_start_year_is_never_plan_eligible() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Watchmen",
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
    )
    issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-watchmen-1",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Watchmen",
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
        run_id="4050-53871",
        run_start_year=None,
    )

    score = score_candidates(
        local,
        [issue],
        MatchingSettings(eligible_score=0, eligible_margin=0),
    )[0]

    assert score.score == 100
    assert score.identity_fields_high
    assert score.eligible is False


def test_collected_local_identity_uses_embedded_writer_and_publisher_evidence() -> None:
    classification = Classification(
        MediaKind.COMIC,
        "collected-edition",
        0.78,
        True,
        (
            ParseHypothesis(
                MediaKind.COMIC,
                "collected-edition",
                0.78,
                title="Mister Miracle",
                series="Mister Miracle",
                year=2019,
            ),
        ),
    )

    local = local_identity(
        classification,
        {
            "comicinfo": {
                "Writer": "Tom King",
                "Publisher": "DC Comics",
                "LanguageISO": "en",
            }
        },
    )

    assert local.creators == ("Tom King",)
    assert local.publisher == "DC Comics"
    assert local.language == "en"


def test_missing_publication_date_never_falls_back_to_comic_run_year() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        year=2026,
    )
    candidates = [
        NormalizedCandidate(
            ProviderName.COMIC_VINE,
            provider_id,
            RecordType.COMIC_ISSUE,
            MediaKind.COMIC,
            "Absolute Batman",
            series_title="Absolute Batman",
            sequence=SequenceNumber.parse("14"),
            run_start_year=run_year,
        )
        for provider_id, run_year in (("correct-run", 2024), ("matching-year-run", 2026))
    ]

    scores = score_candidates(local, candidates, SETTINGS)

    assert scores[0].score == scores[1].score
    assert not scores[0].eligible
    assert all(not any(item.field == "year" for item in score.comparisons) for score in scores)


def test_real_cover_date_and_explicit_run_year_score_separately() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        year=2026,
        run_start_year=2024,
    )
    issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "correct-run",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Absolute Batman",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("14"),
        run_start_year=2024,
        cover_date="2026-01",
        cover_date_precision="month",
        release_date="2025-11-26",
        release_date_precision="day",
    )

    score = score_candidates(local, [issue], SETTINGS)[0]

    year = next(item for item in score.comparisons if item.field == "year")
    run_year = next(item for item in score.comparisons if item.field == "run_start_year")
    assert year.kind is ComparisonKind.EXACT
    assert run_year.kind is ComparisonKind.EXACT


def test_issue_title_conflict_is_explained_but_not_a_hard_identity_contradiction() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Spider-Man Joined the Fantastic Four",
        series_title="What If",
        sequence=SequenceNumber.parse("1"),
    )
    wrong_run_issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-31454",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "What If the Avengers Lost the Evolutionary War?",
        series_title="What If?",
        sequence=SequenceNumber.parse("1"),
    )
    score = score_candidates(local, [wrong_run_issue], SETTINGS)[0]
    comparison = next(item for item in score.comparisons if item.field == "issue_title")
    assert comparison.kind is ComparisonKind.CONFLICT
    assert score.hard_contradiction is False


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


def test_collection_publisher_shorthand_matches_common_imprint_suffixes() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "Book 1",
        creators=("Grant Morrison",),
        series_title="Animal Man",
        sequence=SequenceNumber.parse("1"),
        year=2020,
        publisher="DC",
    )
    candidate = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "animal-man-book-one",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "Animal Man by Grant Morrison Book One",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="DC Comics",
        publication_date="2020",
        series_title="Animal Man",
        sequence=SequenceNumber.parse("1"),
        item_type="collected-edition",
        provider_metadata={"collection_sequence_source": "provider_title"},
    )

    score = score_candidates(local, [candidate], SETTINGS)[0]

    publisher = next(item for item in score.comparisons if item.field == "publisher")
    assert publisher.kind is ComparisonKind.SUPPORTING
    assert publisher.confidence == 1.0
    assert score.score == 100.0


def test_exact_edition_qualifier_outranks_generic_volume_candidate() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "Ultimate Collection Book 2",
        creators=("Grant Morrison",),
        series_title="New X-Men",
        sequence=SequenceNumber.parse("2"),
        year=2009,
        publisher="Marvel",
        edition_qualifiers=("Ultimate Collection",),
    )
    generic = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "generic-volume-2",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "New X-Men by Grant Morrison Vol. 2",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="Marvel",
        publication_date="2009",
        series_title="New X-Men",
        sequence=SequenceNumber.parse("2"),
        item_type="collected-edition",
        provider_metadata={"collection_sequence_source": "provider_title"},
    )
    exact = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "ultimate-volume-2",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "New X-Men by Grant Morrison Ultimate Collection Book 2",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="Marvel",
        publication_date="2009",
        series_title="New X-Men",
        sequence=SequenceNumber.parse("2"),
        item_type="collected-edition",
        provider_metadata={"collection_sequence_source": "provider_title"},
    )

    scores = score_candidates(local, [generic, exact], SETTINGS)

    assert scores[0].candidate.provider_id == "ultimate-volume-2"
    assert scores[0].eligible
    generic_score = next(score for score in scores if score.candidate is generic)
    qualifier = next(
        item for item in generic_score.comparisons if item.field == "edition_qualifier"
    )
    assert qualifier.kind is ComparisonKind.MISSING
    assert not generic_score.identity_fields_high


def test_conflicting_collection_edition_family_blocks_automatic_acceptance() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "DC Black Label Edition",
        creators=("Grant Morrison",),
        series_title="All-Star Superman",
        year=2018,
        publisher="DC",
        edition_qualifiers=("DC Black Label Edition",),
    )
    candidate = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "deluxe",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "All-Star Superman Deluxe Edition",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="DC Comics",
        publication_date="2018",
        series_title="All-Star Superman",
        item_type="collected-edition",
    )

    score = score_candidates(local, [candidate], SETTINGS)[0]

    qualifier = next(item for item in score.comparisons if item.field == "edition_qualifier")
    assert qualifier.kind is ComparisonKind.CONFLICT
    assert qualifier.score_delta == -30
    assert not score.hard_contradiction
    assert not score.identity_fields_high
    assert not score.eligible


def test_collection_publisher_conflict_blocks_automatic_acceptance_without_hard_rejection() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "DC Black Label Edition",
        creators=("Grant Morrison",),
        series_title="All-Star Superman",
        year=2018,
        publisher="DC",
        edition_qualifiers=("DC Black Label Edition",),
    )
    candidate = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "turtleback",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "All-Star Superman (DC Black Label Edition)",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="Turtleback",
        publication_date="2018",
        series_title="All-Star Superman",
        item_type="collected-edition",
    )

    score = score_candidates(local, [candidate], SETTINGS)[0]

    publisher = next(item for item in score.comparisons if item.field == "publisher")
    assert publisher.kind is ComparisonKind.CONFLICT
    assert publisher.score_delta == -20
    assert not score.hard_contradiction
    assert not score.identity_fields_high
    assert not score.eligible


def test_empty_edition_qualifiers_preserve_historical_local_evidence_hash() -> None:
    import hashlib
    import json
    from dataclasses import asdict

    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "",
        series_title="Absolute Batman",
        sequence=SequenceNumber.parse("23"),
        year=2026,
    )
    legacy_fields = asdict(local)
    legacy_fields.pop("edition_qualifiers")
    legacy_payload = json.dumps(legacy_fields, sort_keys=True, default=str)
    legacy_hash = hashlib.sha256(legacy_payload.encode()).hexdigest()

    assert local.evidence_hash() == legacy_hash


def test_real_edition_qualifier_changes_local_evidence_hash() -> None:
    base = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "DC Black Label Edition",
        series_title="All-Star Superman",
        year=2018,
    )
    qualified = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "DC Black Label Edition",
        series_title="All-Star Superman",
        year=2018,
        edition_qualifiers=("DC Black Label Edition",),
    )

    assert qualified.evidence_hash() != base.evidence_hash()


def test_local_identity_carries_structured_edition_qualifiers() -> None:
    classification = Classification(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        False,
        (
            ParseHypothesis(
                MediaKind.COMIC,
                "collected-edition",
                0.98,
                title="DC Black Label Edition",
                series="All-Star Superman",
                year=2018,
                edition_qualifiers=("DC Black Label Edition",),
            ),
        ),
    )

    local = local_identity(classification, {})

    assert local.edition_qualifiers == ("DC Black Label Edition",)
