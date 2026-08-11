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
