from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Self

from .domain import SequenceNumber
from .matching import LocalIdentity
from .providers.base import Provider, ProviderError
from .providers.comic_vine import ComicVineProvider
from .providers.models import NormalizedCandidate, ProviderName, SearchQuery


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    candidates: tuple[NormalizedCandidate, ...]
    queries: tuple[str, ...]
    unavailable: tuple[str, ...]
    available: tuple[ProviderName, ...] = ()


@dataclass(slots=True)
class CandidateSession:
    run_start_hints: dict[str, int]
    run_max_sequences: dict[str, SequenceNumber]
    resolved_runs: dict[str, NormalizedCandidate | None]
    run_resolution_queries: int = 0
    run_disambiguation_queries: int = 0
    repeated_run_queries_avoided: int = 0
    run_issue_queries: int = 0
    max_disambiguation_queries: int = 40
    max_ambiguous_runs: int = 5
    disambiguation_budget_exhausted: int = 0

    @classmethod
    def from_local_identities(cls, values: list[LocalIdentity]) -> Self:
        run_start_hints: dict[str, list[int]] = {}
        sequences: dict[str, list[SequenceNumber]] = {}
        for local in values:
            if (
                local.kind.value == "comic"
                and local.subtype != "collected-edition"
                and local.series_title
                and local.run_start_year
            ):
                run_start_hints.setdefault(_run_key(local.series_title), []).append(
                    local.run_start_year
                )
            if (
                local.kind.value == "comic"
                and local.subtype != "collected-edition"
                and local.series_title
                and local.sequence
            ):
                sequences.setdefault(_run_key(local.series_title), []).append(local.sequence)
        return cls(
            {key: min(candidates) for key, candidates in run_start_hints.items()},
            {
                key: max(candidates, key=lambda item: item.sort_key)
                for key, candidates in sequences.items()
            },
            {},
        )

    def search_comic_issue(
        self,
        local: LocalIdentity,
        provider: ComicVineProvider,
    ) -> tuple[list[NormalizedCandidate], list[str]]:
        if not local.series_title or not local.sequence:
            return provider.search(_structured_query(local)), ["comic_vine:structured"]
        key = _run_key(local.series_title)
        queries: list[str] = []
        if key in self.resolved_runs:
            self.repeated_run_queries_avoided += 1
            queries.append("comic_vine:run-reused")
        else:
            hint = local.run_start_year or self.run_start_hints.get(key)
            run_query = SearchQuery(
                local.kind,
                local.series_title,
                series_title=local.series_title,
                run_start_year=hint,
                item_type="run",
            )
            runs = provider.search_runs(run_query)
            self.run_resolution_queries += 1
            queries.append("comic_vine:run-resolved")
            matching_runs = _matching_runs(local.series_title, hint, runs)
            selected = matching_runs[0] if len(matching_runs) == 1 else None
            issue_candidates: list[NormalizedCandidate] = []
            if selected is None and matching_runs:
                selected, issue_candidates = self._disambiguate_run(
                    key, local, provider, matching_runs
                )
            self.resolved_runs[key] = selected
            if issue_candidates:
                queries.append("comic_vine:issue-evidence")
                return issue_candidates, queries
        run = self.resolved_runs[key]
        if run is None:
            queries.append("comic_vine:structured-fallback")
            return _identity_candidates(local, provider.search(_structured_query(local))), queries
        self.run_issue_queries += 1
        queries.append(f"comic_vine:issue-in-run:{run.provider_id}")
        return provider.search_issue_in_run(run, local.sequence), queries

    def seed_resolved_run(self, series_title: str, run: NormalizedCandidate) -> None:
        if run.record_type.value != "comic_run":
            raise ValueError("candidate-session run seed must be a comic run")
        self.resolved_runs[_run_key(series_title)] = run

    def metrics(self) -> dict[str, int]:
        return {
            "run_resolution_queries": self.run_resolution_queries,
            "run_disambiguation_queries": self.run_disambiguation_queries,
            "repeated_run_queries_avoided": self.repeated_run_queries_avoided,
            "run_issue_queries": self.run_issue_queries,
            "resolved_runs": sum(value is not None for value in self.resolved_runs.values()),
            "ambiguous_runs": sum(value is None for value in self.resolved_runs.values()),
            "disambiguation_budget_exhausted": self.disambiguation_budget_exhausted,
        }

    def _disambiguate_run(
        self,
        key: str,
        local: LocalIdentity,
        provider: ComicVineProvider,
        runs: list[NormalizedCandidate],
    ) -> tuple[NormalizedCandidate | None, list[NormalizedCandidate]]:
        runs = runs[: self.max_ambiguous_runs]
        if not local.sequence:
            return None, []
        found: list[tuple[NormalizedCandidate, NormalizedCandidate]] = []
        for run in runs:
            issues = self._probe_issue(provider, run, local.sequence)
            if issues is None:
                return None, []
            found.extend((run, issue) for issue in issues)
        run_ids = {run.provider_id for run, _ in found}
        if len(run_ids) == 1:
            return next(run for run, _ in found), [issue for _, issue in found]

        publication_matches = [
            pair for pair in found if local.year and _publication_year(pair[1]) == local.year
        ]
        matching_run_ids = {run.provider_id for run, _ in publication_matches}
        if len(matching_run_ids) == 1:
            selected_id = next(iter(matching_run_ids))
            return (
                next(run for run, _ in publication_matches if run.provider_id == selected_id),
                [issue for _, issue in publication_matches],
            )

        if local.title and _run_key(local.title) != _run_key(local.series_title or ""):
            ranked = sorted(
                ((_title_similarity(local.title, issue.title), run, issue) for run, issue in found),
                key=lambda item: (-item[0], item[1].provider_id, item[2].provider_id),
            )
            runner = ranked[1][0] if len(ranked) > 1 else 0.0
            if ranked and ranked[0][0] >= 0.75 and ranked[0][0] - runner >= 0.15:
                return ranked[0][1], [ranked[0][2]]
        return None, [issue for _, issue in found]

    def _probe_issue(
        self,
        provider: ComicVineProvider,
        run: NormalizedCandidate,
        sequence: SequenceNumber,
    ) -> list[NormalizedCandidate] | None:
        if self.run_disambiguation_queries >= self.max_disambiguation_queries:
            self.disambiguation_budget_exhausted += 1
            return None
        self.run_disambiguation_queries += 1
        return provider.search_issue_in_run(run, sequence)


def generate_candidates(
    local: LocalIdentity,
    providers: tuple[Provider, ...],
    session: CandidateSession | None = None,
) -> CandidateGeneration:
    output: dict[str, NormalizedCandidate] = {}
    queries: list[str] = []
    unavailable: list[str] = []
    available: list[ProviderName] = []
    for provider in providers:
        status = provider.status()
        if local.kind.value == "book" and status.provider is ProviderName.COMIC_VINE:
            continue
        if local.kind.value == "comic" and status.provider is not ProviderName.COMIC_VINE:
            continue
        if not status.enabled and "cached" not in status.capabilities:
            unavailable.append(f"{status.provider.value}: {status.detail}")
            continue
        try:
            for identifier in local.identifiers:
                queries.append(f"{status.provider.value}:identifier:{identifier.scheme}")
                _merge(output, provider.lookup_identifier(identifier))
            provider_found = any(item.provider is status.provider for item in output.values())
            if provider_found:
                available.append(status.provider)
                continue
            if (
                session is not None
                and isinstance(provider, ComicVineProvider)
                and local.subtype != "collected-edition"
            ):
                values, comic_queries = session.search_comic_issue(local, provider)
                queries.extend(comic_queries)
                _merge(output, values)
            else:
                queries.append(f"{status.provider.value}:structured")
                _merge(output, provider.search(_structured_query(local)))
            available.append(status.provider)
            provider_found = any(item.provider is status.provider for item in output.values())
            if not provider_found and (local.creators or local.series_title):
                relaxed = SearchQuery(
                    local.kind,
                    local.title,
                    series_title=local.series_title,
                    sequence=local.sequence,
                    item_type=local.subtype,
                    relaxed=True,
                )
                queries.append(f"{status.provider.value}:relaxed")
                _merge(output, provider.search(relaxed))
        except ProviderError as exc:
            unavailable.append(f"{status.provider.value}: {exc}")
    return CandidateGeneration(
        tuple(_identity_candidates(local, list(output.values()))),
        tuple(queries),
        tuple(unavailable),
        tuple(available),
    )


def _merge(destination: dict[str, NormalizedCandidate], values: list[NormalizedCandidate]) -> None:
    for item in values:
        destination[item.key] = item


def _structured_query(local: LocalIdentity) -> SearchQuery:
    return SearchQuery(
        media_kind=local.kind,
        title=local.title,
        creators=local.creators,
        identifiers=local.identifiers,
        series_title=local.series_title,
        sequence=local.sequence,
        run_start_year=local.run_start_year,
        item_type=local.subtype,
    )


def _matching_runs(
    series_title: str,
    run_start_year: int | None,
    candidates: list[NormalizedCandidate],
) -> list[NormalizedCandidate]:
    title = _run_key(series_title)
    matching = [
        candidate
        for candidate in candidates
        if candidate.record_type.value == "comic_run"
        and candidate.series_title
        and _run_key(candidate.series_title) == title
    ]
    if run_start_year is not None:
        year_matches = [
            candidate for candidate in matching if candidate.run_start_year == run_start_year
        ]
        if year_matches:
            return year_matches
    return matching


def _identity_candidates(
    local: LocalIdentity, candidates: list[NormalizedCandidate]
) -> list[NormalizedCandidate]:
    if local.kind.value != "comic":
        return candidates
    if local.subtype == "collected-edition":
        return [item for item in candidates if item.record_type.value == "comic_collection"]
    return [item for item in candidates if item.record_type.value == "comic_issue"]


def _publication_year(candidate: NormalizedCandidate) -> int | None:
    match = re.match(r"(\d{4})", candidate.publication_date or "")
    return int(match.group(1)) if match else None


def _run_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _run_key(left), _run_key(right)).ratio()
