from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Self

from .collection_candidates import adapt_collection_candidate, collection_number_word
from .domain import MediaKind, SequenceNumber
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
            pair for pair in found if local.year and _comic_cover_year(pair[1]) == local.year
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

        # A same-named collected/translated run can legitimately contain issue #1 too.
        # Use the highest issue number present in the current local series group as
        # additional run evidence before giving up and caching the group as ambiguous.
        # This is intentionally only a discriminator: absence of that issue does not
        # prove a run is wrong unless another candidate run does contain it.
        max_sequence = self.run_max_sequences.get(key)
        if max_sequence and max_sequence.normalized != local.sequence.normalized:
            candidate_runs = {run.provider_id: run for run, _ in found}
            max_issue_runs: list[NormalizedCandidate] = []
            for run in candidate_runs.values():
                issues = self._probe_issue(provider, run, max_sequence)
                if issues is None:
                    return None, [issue for _, issue in found]
                if issues:
                    max_issue_runs.append(run)
            if len(max_issue_runs) == 1:
                selected = max_issue_runs[0]
                return selected, [
                    issue for run, issue in found if run.provider_id == selected.provider_id
                ]

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
    collected = local.kind is MediaKind.COMIC and local.subtype == "collected-edition"

    for provider in providers:
        status = provider.status()
        if local.kind is MediaKind.BOOK and status.provider is ProviderName.COMIC_VINE:
            continue
        if (
            local.kind is MediaKind.COMIC
            and not collected
            and status.provider is not ProviderName.COMIC_VINE
        ):
            continue
        if collected and status.provider is ProviderName.COMIC_VINE:
            # Comic Vine's public volume resource has no collection-format field.
            # Do not guess that a same-titled volume is a TPB/hardcover/omnibus.
            continue
        if not status.enabled and "cached" not in status.capabilities:
            unavailable.append(f"{status.provider.value}: {status.detail}")
            continue
        try:
            for identifier in local.identifiers:
                queries.append(f"{status.provider.value}:identifier:{identifier.scheme}")
                values = provider.lookup_identifier(identifier)
                _merge(
                    output,
                    _collection_candidates(local, values) if collected else values,
                )
            provider_found = any(item.provider is status.provider for item in output.values())
            if provider_found:
                available.append(status.provider)
                continue
            if session is not None and isinstance(provider, ComicVineProvider):
                values, comic_queries = session.search_comic_issue(local, provider)
                queries.extend(comic_queries)
                _merge(output, values)
            elif collected:
                any_response, collection_errors = _search_collection_provider(
                    local, provider, status.provider, output, queries
                )
                provider_found = any(
                    item.provider is status.provider for item in output.values()
                )
                if any_response:
                    available.append(status.provider)
                if collection_errors and not provider_found:
                    unavailable.append(
                        f"{status.provider.value}: partial collection search failure: "
                        f"{collection_errors[-1]}"
                    )
                continue
            else:
                query = _structured_query(local)
                queries.append(f"{status.provider.value}:structured")
                values = provider.search(query)
                _merge(output, values)
            available.append(status.provider)
            provider_found = any(item.provider is status.provider for item in output.values())
            if not collected and not provider_found and (local.creators or local.series_title):
                relaxed = SearchQuery(
                    local.kind,
                    local.title,
                    series_title=local.series_title,
                    sequence=local.sequence,
                    item_type=local.subtype,
                    relaxed=True,
                )
                queries.append(f"{status.provider.value}:relaxed")
                values = provider.search(relaxed)
                _merge(output, values)
        except ProviderError as exc:
            unavailable.append(f"{status.provider.value}: {exc}")
    return CandidateGeneration(
        tuple(_identity_candidates(local, list(output.values()))),
        tuple(queries),
        tuple(unavailable),
        tuple(available),
    )


def _search_collection_provider(
    local: LocalIdentity,
    provider: Provider,
    provider_name: ProviderName,
    output: dict[str, NormalizedCandidate],
    queries: list[str],
) -> tuple[bool, list[str]]:
    """Run collection query variants independently so one transient failure is not fatal.

    Collection recall deliberately uses a bounded query ladder because catalogue
    providers vary in how creator credits and volume numbers appear in titles. A
    temporary HTTP failure for one spelling must not prevent later, more exact
    variants from being attempted. The provider client already performs its own
    per-request retry; this function isolates failures between distinct queries.
    """
    any_response = False
    errors: list[str] = []
    for label, query in _collection_book_queries(local):
        queries.append(f"{provider_name.value}:collection:{label}")
        try:
            values = provider.search(query)
        except ProviderError as exc:
            errors.append(f"{label}: {exc}")
            continue
        any_response = True
        _merge(output, _collection_candidates(local, values))

    provider_found = any(item.provider is provider_name for item in output.values())
    if not provider_found:
        relaxed = _collection_relaxed_query(local)
        queries.append(f"{provider_name.value}:collection:relaxed")
        try:
            values = provider.search(relaxed)
        except ProviderError as exc:
            errors.append(f"relaxed: {exc}")
        else:
            any_response = True
            _merge(output, _collection_candidates(local, values))
    return any_response, errors


def _collection_candidates(
    local: LocalIdentity, values: list[NormalizedCandidate]
) -> list[NormalizedCandidate]:
    output: list[NormalizedCandidate] = []
    for candidate in values:
        adapted = adapt_collection_candidate(
            candidate,
            series_title=local.series_title,
            sequence=local.sequence,
            item_type=local.subtype,
        )
        if adapted is not None:
            output.append(adapted)
    return output


def _collection_book_query(local: LocalIdentity) -> SearchQuery:
    series = (local.series_title or "").strip()
    title = local.title.strip()
    if series and title and not title.casefold().startswith(series.casefold()):
        title = f"{series} {title}"
    elif not title:
        title = series
    return SearchQuery(
        MediaKind.BOOK,
        title,
        creators=local.creators,
        identifiers=local.identifiers,
        item_type="collected-edition",
    )


def _collection_book_queries(local: LocalIdentity) -> tuple[tuple[str, SearchQuery], ...]:
    """Build a small high-precision query ladder for provider title conventions.

    Comic collections are frequently catalogued with creator credits embedded in
    the title (``Animal Man by Grant Morrison Book One``) even when the local
    release filename separates creator evidence from the title. Providers also
    vary between numeric and word-spelled collection numbers. Search those
    documented title forms without weakening identity scoring or silently
    importing a collection number from local evidence.
    """
    primary = _collection_book_query(local)
    variants: list[tuple[str, SearchQuery]] = [("structured", primary)]
    seen = {(primary.title.casefold(), primary.creators, primary.relaxed)}
    if local.creators:
        title_only = SearchQuery(
            MediaKind.BOOK,
            primary.title,
            creators=(),
            identifiers=local.identifiers,
            item_type="collected-edition",
        )
        variants.append(("title-only", title_only))
        seen.add((title_only.title.casefold(), title_only.creators, title_only.relaxed))

    series = (local.series_title or "").strip()
    creator = local.creators[0].strip() if local.creators else ""
    if series and creator:
        suffix = local.title.strip()
        if suffix.casefold().startswith(series.casefold()):
            suffix = suffix[len(series) :].strip(" :-")
        if suffix.casefold() == series.casefold():
            suffix = ""
        inline_title = " ".join(
            part for part in (series, f"by {creator}", suffix) if part
        )
        _append_collection_query(
            variants,
            seen,
            "creator-inline",
            inline_title,
            local,
        )
        dashed_title = re.sub(
            r"\b(Collection)\s+(Book|Volume)\b",
            r"\1 - \2",
            inline_title,
            flags=re.IGNORECASE,
        )
        _append_collection_query(
            variants,
            seen,
            "creator-inline-dashed",
            dashed_title,
            local,
        )
        sequence = local.sequence
        word = collection_number_word(sequence)
        if sequence is not None and word:
            word_title = re.sub(
                rf"\b(Book|Volume|Vol\.?)\s*0*{re.escape(sequence.normalized)}\b",
                lambda match: f"{match.group(1)} {word.title()}",
                inline_title,
                flags=re.IGNORECASE,
            )
            _append_collection_query(
                variants,
                seen,
                "creator-inline-word-number",
                word_title,
                local,
            )
    return tuple(variants)


def _append_collection_query(
    variants: list[tuple[str, SearchQuery]],
    seen: set[tuple[str, tuple[str, ...], bool]],
    label: str,
    title: str,
    local: LocalIdentity,
) -> None:
    key = (title.casefold(), local.creators, False)
    if not title or key in seen:
        return
    seen.add(key)
    variants.append(
        (
            label,
            SearchQuery(
                MediaKind.BOOK,
                title,
                creators=local.creators,
                identifiers=local.identifiers,
                item_type="collected-edition",
            ),
        )
    )


def _collection_relaxed_query(local: LocalIdentity) -> SearchQuery:
    title = (local.series_title or local.title).strip()
    return SearchQuery(
        MediaKind.BOOK,
        title,
        creators=(),
        identifiers=local.identifiers,
        item_type="collected-edition",
        relaxed=True,
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


def _comic_cover_year(candidate: NormalizedCandidate) -> int | None:
    match = re.match(r"(\d{4})", candidate.cover_date or "")
    return int(match.group(1)) if match else None


def _run_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _run_key(left), _run_key(right)).ratio()
