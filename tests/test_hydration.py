from __future__ import annotations

from dataclasses import replace

from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.hydration import hydrate_candidate, merge_exact_candidate
from kavita_ingest.providers.base import ProviderError, ProviderStatus
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)


def _candidate(**changes: object) -> NormalizedCandidate:
    candidate = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-1145497",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Abomination, Conclusion",
        identifiers=(Identifier("comic_vine", "4000-1145497"),),
        publisher="DC Comics",
        release_date="2025-11-26",
        release_date_precision="day",
        cover_date="2026-01",
        cover_date_precision="month",
        series_title="Absolute Batman",
        run_start_year=2024,
        sequence=SequenceNumber.parse("14"),
        item_type="issue",
        run_id="4050-160294",
        provider_schema_version=3,
    )
    return replace(candidate, **changes)


class DetailProvider:
    name = ProviderName.COMIC_VINE

    def __init__(self, detail: list[NormalizedCandidate] | ProviderError) -> None:
        self.detail = detail
        self.fetches = 0

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, True, "fixture", ("exact_fetch",))

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        del query
        return []

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        assert provider_id == "4000-1145497"
        self.fetches += 1
        if isinstance(self.detail, ProviderError):
            raise self.detail
        return self.detail

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        del identifier
        return []


def test_exact_detail_enriches_without_erasing_resolved_run_context() -> None:
    discovery = _candidate()
    exact = _candidate(
        creators=(
            Contributor("Scott Snyder", "writer"),
            Contributor("Nick Dragotta", "artist"),
        ),
        run_start_year=None,
    )

    result = merge_exact_candidate(discovery, exact)

    assert result.status == "hydrated"
    assert result.candidate.run_start_year == 2024
    assert result.candidate.creators == exact.creators
    assert result.changes == ("contributors",)


def test_exact_detail_unions_contributors_and_identifiers_without_duplicates() -> None:
    shared = Contributor("Scott Snyder", "writer")
    discovery = _candidate(creators=(shared,))
    exact = _candidate(
        creators=(shared, Contributor("Frank Martin", "colorist")),
        identifiers=(
            Identifier("comic_vine", "4000-1145497"),
            Identifier("legacy", "issue-14"),
        ),
    )

    result = merge_exact_candidate(discovery, exact)

    assert result.candidate.creators == (
        shared,
        Contributor("Frank Martin", "colorist"),
    )
    assert result.candidate.identifiers[-1] == Identifier("legacy", "issue-14")


def test_exact_publisher_change_is_metadata_not_identity_conflict() -> None:
    discovery = _candidate(publisher="Independently Published")
    exact = _candidate(publisher="Amazon Digital Services LLC KDP")

    result = merge_exact_candidate(discovery, exact)

    assert result.status == "hydrated"
    assert result.candidate.publisher == "Amazon Digital Services LLC KDP"
    assert "publisher" in result.changes


def test_material_exact_identity_conflict_never_overwrites_selection() -> None:
    discovery = _candidate()
    exact = _candidate(sequence=SequenceNumber.parse("15"), title="Different Issue")

    result = merge_exact_candidate(discovery, exact)

    assert result.status == "conflict"
    assert result.candidate is discovery
    assert any("issue number differs" in item for item in result.conflicts)
    assert any("title differs" in item for item in result.conflicts)


def test_hydration_failure_retains_sparse_candidate_for_explicit_policy_choice() -> None:
    discovery = _candidate()
    provider = DetailProvider(ProviderError("temporary outage"))

    result = hydrate_candidate(discovery, (provider,))

    assert result.status == "unavailable"
    assert result.candidate is discovery
    assert result.error == "temporary outage"
    assert provider.fetches == 1


class BookEditionDetailProvider:
    name = ProviderName.GOOGLE_BOOKS

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, True, "fixture", ("exact_fetch",))

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        del query
        return []

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        assert provider_id == "collection-1"
        return [
            NormalizedCandidate(
                ProviderName.GOOGLE_BOOKS,
                "collection-1",
                RecordType.BOOK_EDITION,
                MediaKind.BOOK,
                "Animal Man by Grant Morrison Book 1",
                creators=(Contributor("Grant Morrison", "author"),),
                publisher="DC Comics",
                publication_date="2020-01-01",
                edition_id="collection-1",
            )
        ]

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        del identifier
        return []


def test_hydration_reapplies_comic_collection_semantics_to_book_edition_detail() -> None:
    selected = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "collection-1",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "Animal Man by Grant Morrison Book 1",
        creators=(Contributor("Grant Morrison", "writer"),),
        series_title="Animal Man",
        sequence=SequenceNumber.parse("1"),
        item_type="collected-edition",
        provider_metadata={
            "collection_adapter": "book_edition",
            "collection_source_record_type": "book_edition",
            "collection_series_source": "local",
            "collection_sequence_source": "local",
        },
    )

    result = hydrate_candidate(selected, (BookEditionDetailProvider(),))

    assert result.status == "hydrated"
    assert result.candidate.record_type is RecordType.COMIC_COLLECTION
    assert result.candidate.media_kind is MediaKind.COMIC
    assert result.candidate.series_title == "Animal Man"
    assert result.candidate.sequence == SequenceNumber.parse("1")
    assert result.candidate.creators == (Contributor("Grant Morrison", "writer"),)
    assert result.candidate.publisher == "DC Comics"

class SparseBookTwoDetailProvider:
    name = ProviderName.OPEN_LIBRARY

    def __init__(self, title: str = "Animal Man by Grant Morrison") -> None:
        self.title = title

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, True, "fixture", ("exact_fetch",))

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        del query
        return []

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        assert provider_id == "OL29876683M"
        return [
            NormalizedCandidate(
                ProviderName.OPEN_LIBRARY,
                "OL29876683M",
                RecordType.BOOK_EDITION,
                MediaKind.BOOK,
                self.title,
                creators=(Contributor("Grant Morrison", "author"),),
                publisher="DC Comics",
                publication_date="2020",
                edition_id="OL29876683M",
            )
        ]

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        del identifier
        return []


def _animal_man_book_two_selection() -> NormalizedCandidate:
    return NormalizedCandidate(
        ProviderName.OPEN_LIBRARY,
        "OL29876683M",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "Animal Man by Grant Morrison",
        subtitle="Book Two",
        creators=(Contributor("Grant Morrison", "writer"),),
        publisher="DC Comics",
        publication_date="2020",
        series_title="Animal Man",
        sequence=SequenceNumber.parse("2"),
        item_type="collected-edition",
        edition_id="OL29876683M",
        provider_metadata={
            "collection_adapter": "book_edition",
            "collection_source_record_type": "book_edition",
            "collection_series_source": "local",
            "collection_sequence_source": "provider_title",
        },
    )


def test_collection_hydration_keeps_verified_sequence_when_exact_detail_is_sparse() -> None:
    selected = _animal_man_book_two_selection()

    result = hydrate_candidate(selected, (SparseBookTwoDetailProvider(),))

    assert result.status == "hydrated"
    assert result.candidate.record_type is RecordType.COMIC_COLLECTION
    assert result.candidate.media_kind is MediaKind.COMIC
    assert result.candidate.sequence == SequenceNumber.parse("2")
    assert result.candidate.creators == (Contributor("Grant Morrison", "writer"),)


def test_collection_hydration_still_rejects_explicit_exact_sequence_contradiction() -> None:
    selected = _animal_man_book_two_selection()

    result = hydrate_candidate(
        selected,
        (SparseBookTwoDetailProvider("Animal Man by Grant Morrison Book Three"),),
    )

    assert result.status == "conflict"
    assert any("issue number differs" in item for item in result.conflicts)
