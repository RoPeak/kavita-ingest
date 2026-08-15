from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Self

from .collection_candidates import (
    adapt_collection_candidate,
    collection_candidate_rejection_reason,
    collection_number_word,
)
from .domain import MediaKind, SequenceNumber
from .matching import LocalIdentity
from .providers.base import Provider, ProviderError
from .providers.comic_vine import ComicVineProvider
from .providers.models import (
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: ProviderName
    strategy: str
    outcome: str
    raw_count: int = 0
    accepted_count: int = 0
    rejection_counts: tuple[tuple[str, int], ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    candidates: tuple[NormalizedCandidate, ...]
    queries: tuple[str, ...]
    unavailable: tuple[str, ...]
    available: tuple[ProviderName, ...] = ()
    attempts: tuple[ProviderAttempt, ...] = ()


@dataclass(slots=True)
class CandidateSession:
    run_start_hints: dict[str, int]
    run_max_sequences: dict[str, SequenceNumber]
    resolved_runs: dict[str, NormalizedCandidate | None]
    known_runs: dict[tuple[ProviderName, str], NormalizedCandidate] = field(default_factory=dict)
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
        key = self._resolved_key(local.series_title)
        queries: list[str] = []
        if key in self.resolved_runs:
            self.repeated_run_queries_avoided += 1
            queries.append("comic_vine:run-reused")
            reused = self.resolved_runs[key]
            if reused is not None:
                self._store_resolution(local.series_title, reused)
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
            self._store_resolution(local.series_title, selected)
            if issue_candidates:
                self._remember_issue_runs(issue_candidates)
                queries.append("comic_vine:issue-evidence")
                return issue_candidates, queries
        key = self._resolved_key(local.series_title)
        run = self.resolved_runs[key]
        if run is None:
            queries.append("comic_vine:structured-fallback")
            fallback = _identity_candidates(local, provider.search(_structured_query(local)))
            self._remember_issue_runs(fallback)
            return fallback, queries
        self.run_issue_queries += 1
        queries.append(f"comic_vine:issue-in-run:{run.provider_id}")
        issues = provider.search_issue_in_run(run, local.sequence)
        self._remember_issue_runs(issues)
        return issues, queries

    def seed_resolved_run(self, series_title: str, run: NormalizedCandidate) -> None:
        if run.record_type.value != "comic_run":
            raise ValueError("candidate-session run seed must be a comic run")
        self._remember_run(run)
        self._store_resolution(series_title, run)

    def _resolved_key(self, series_title: str) -> str:
        keys = _run_alias_keys(series_title)
        for key in keys:
            if key in self.resolved_runs and self.resolved_runs[key] is not None:
                return key
        for key in keys:
            if key in self.resolved_runs:
                return key
        return _run_key(series_title)

    def _store_resolution(
        self,
        series_title: str,
        run: NormalizedCandidate | None,
    ) -> None:
        if run is None:
            self.resolved_runs[_run_key(series_title)] = None
            return
        self.resolved_runs[_run_key(series_title)] = run
        self._remember_run(run)
        for value in (run.series_title, run.title):
            if value:
                self.resolved_runs[_run_key(value)] = run

    def _remember_run(self, run: NormalizedCandidate) -> None:
        self.known_runs[(run.provider, run.provider_id)] = run

    def _remember_issue_runs(self, candidates: list[NormalizedCandidate]) -> None:
        aliases: dict[str, dict[str, NormalizedCandidate]] = {}
        for candidate in candidates:
            run = comic_run_context(candidate)
            if run is None:
                continue
            known = self.known_runs.get((run.provider, run.provider_id))
            if known is not None:
                run = known
            else:
                self._remember_run(run)
            for value in (candidate.series_title, run.series_title, run.title):
                if not value:
                    continue
                aliases.setdefault(_run_key(value), {})[run.provider_id] = run
        for key, runs in aliases.items():
            if len(runs) != 1:
                continue
            run = next(iter(runs.values()))
            if key not in self.resolved_runs:
                self.resolved_runs[key] = run
                continue
            current = self.resolved_runs[key]
            if current is not None and current.provider_id == run.provider_id:
                self.resolved_runs[key] = run

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
    attempts: list[ProviderAttempt] = []
    collected = local.kind is MediaKind.COMIC and local.subtype == "collected-edition"

    for provider in providers:
        status = provider.status()
        if local.kind is MediaKind.BOOK and status.provider is ProviderName.COMIC_VINE:
            attempts.append(
                ProviderAttempt(
                    status.provider,
                    "provider-routing",
                    "skipped",
                    detail="media_kind_not_supported",
                )
            )
            continue
        if (
            local.kind is MediaKind.COMIC
            and not collected
            and status.provider is not ProviderName.COMIC_VINE
        ):
            attempts.append(
                ProviderAttempt(
                    status.provider,
                    "provider-routing",
                    "skipped",
                    detail="media_kind_not_supported",
                )
            )
            continue
        if collected and status.provider is ProviderName.COMIC_VINE:
            # Comic Vine's public volume resource has no collection-format field.
            # Do not guess that a same-titled volume is a TPB/hardcover/omnibus.
            attempts.append(
                ProviderAttempt(
                    status.provider,
                    "collection-format",
                    "skipped",
                    detail="unsupported_collection_format",
                )
            )
            continue
        if not status.enabled and "cached" not in status.capabilities:
            unavailable.append(f"{status.provider.value}: {status.detail}")
            attempts.append(
                ProviderAttempt(
                    status.provider,
                    "provider-status",
                    "unavailable",
                    detail="provider_unavailable",
                )
            )
            continue
        try:
            for identifier in local.identifiers:
                strategy = f"identifier:{identifier.scheme}"
                queries.append(f"{status.provider.value}:{strategy}")
                values = provider.lookup_identifier(identifier)
                accepted, rejection_counts = _diagnostic_identity_candidates(
                    local, values, collected=collected
                )
                attempts.append(
                    ProviderAttempt(
                        status.provider,
                        strategy,
                        "ok",
                        raw_count=len(values),
                        accepted_count=len(accepted),
                        rejection_counts=rejection_counts,
                    )
                )
                _merge(output, accepted if collected else values)
            provider_found = any(item.provider is status.provider for item in output.values())
            if provider_found:
                available.append(status.provider)
                continue
            if session is not None and isinstance(provider, ComicVineProvider):
                values, comic_queries = session.search_comic_issue(local, provider)
                queries.extend(comic_queries)
                accepted, rejection_counts = _diagnostic_identity_candidates(
                    local, values, collected=False
                )
                attempts.append(
                    ProviderAttempt(
                        status.provider,
                        "comic-issue",
                        "ok",
                        raw_count=len(values),
                        accepted_count=len(accepted),
                        rejection_counts=rejection_counts,
                    )
                )
                _merge(output, values)
            elif collected:
                any_response, collection_errors = _search_collection_provider(
                    local,
                    provider,
                    status.provider,
                    output,
                    queries,
                    attempts,
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
                strategy = "structured"
                queries.append(f"{status.provider.value}:{strategy}")
                values = provider.search(query)
                accepted, rejection_counts = _diagnostic_identity_candidates(
                    local, values, collected=False
                )
                attempts.append(
                    ProviderAttempt(
                        status.provider,
                        strategy,
                        "ok",
                        raw_count=len(values),
                        accepted_count=len(accepted),
                        rejection_counts=rejection_counts,
                    )
                )
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
                strategy = "relaxed"
                queries.append(f"{status.provider.value}:{strategy}")
                values = provider.search(relaxed)
                accepted, rejection_counts = _diagnostic_identity_candidates(
                    local, values, collected=False
                )
                attempts.append(
                    ProviderAttempt(
                        status.provider,
                        strategy,
                        "ok",
                        raw_count=len(values),
                        accepted_count=len(accepted),
                        rejection_counts=rejection_counts,
                    )
                )
                _merge(output, values)
        except ProviderError as exc:
            unavailable.append(f"{status.provider.value}: {exc}")
            attempts.append(
                ProviderAttempt(
                    status.provider,
                    "provider-request",
                    "error",
                    detail="provider_failure",
                )
            )
    return CandidateGeneration(
        tuple(_identity_candidates(local, list(output.values()))),
        tuple(queries),
        tuple(unavailable),
        tuple(dict.fromkeys(available)),
        tuple(attempts),
    )


def _search_collection_provider(
    local: LocalIdentity,
    provider: Provider,
    provider_name: ProviderName,
    output: dict[str, NormalizedCandidate],
    queries: list[str],
    attempts: list[ProviderAttempt],
) -> tuple[bool, list[str]]:
    """Run collection query variants independently so one transient failure is not fatal."""
    any_response = False
    errors: list[str] = []
    for label, query in _collection_book_queries(local):
        strategy = f"collection:{label}"
        queries.append(f"{provider_name.value}:{strategy}")
        try:
            values = provider.search(query)
        except ProviderError as exc:
            errors.append(f"{label}: {exc}")
            attempts.append(
                ProviderAttempt(
                    provider_name,
                    strategy,
                    "error",
                    detail="provider_failure",
                )
            )
            continue
        any_response = True
        accepted, rejection_counts = _diagnostic_identity_candidates(
            local, values, collected=True
        )
        attempts.append(
            ProviderAttempt(
                provider_name,
                strategy,
                "ok",
                raw_count=len(values),
                accepted_count=len(accepted),
                rejection_counts=rejection_counts,
            )
        )
        _merge(output, accepted)

    provider_found = any(item.provider is provider_name for item in output.values())
    if not provider_found:
        relaxed = _collection_relaxed_query(local)
        strategy = "collection:relaxed"
        queries.append(f"{provider_name.value}:{strategy}")
        try:
            values = provider.search(relaxed)
        except ProviderError as exc:
            errors.append(f"relaxed: {exc}")
            attempts.append(
                ProviderAttempt(
                    provider_name,
                    strategy,
                    "error",
                    detail="provider_failure",
                )
            )
        else:
            any_response = True
            accepted, rejection_counts = _diagnostic_identity_candidates(
                local, values, collected=True
            )
            attempts.append(
                ProviderAttempt(
                    provider_name,
                    strategy,
                    "ok",
                    raw_count=len(values),
                    accepted_count=len(accepted),
                    rejection_counts=rejection_counts,
                )
            )
            _merge(output, accepted)
    return any_response, errors


def _diagnostic_identity_candidates(
    local: LocalIdentity,
    values: list[NormalizedCandidate],
    *,
    collected: bool,
) -> tuple[list[NormalizedCandidate], tuple[tuple[str, int], ...]]:
    if collected:
        accepted: list[NormalizedCandidate] = []
        reasons: Counter[str] = Counter()
        for candidate in values:
            reason = collection_candidate_rejection_reason(candidate, local.sequence)
            if reason is not None:
                reasons[reason] += 1
                continue
            adapted = adapt_collection_candidate(
                candidate,
                series_title=local.series_title,
                sequence=local.sequence,
                item_type=local.subtype,
            )
            if adapted is None:
                reasons["wrong_record_type"] += 1
            else:
                accepted.append(adapted)
        return accepted, tuple(sorted(reasons.items()))

    accepted = _identity_candidates(local, values)
    rejected = len(values) - len(accepted)
    counts = (("wrong_record_type", rejected),) if rejected else ()
    return accepted, counts



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


def _run_alias_keys(value: str) -> tuple[str, ...]:
    """Return conservative in-session aliases for a local comic series.

    Embedded ComicInfo sometimes appends creator credits to the actual series
    name (for example ``Oblivion Song By Kirkman & De Felici``). Provider run
    identity is stronger than that local decoration, but broad fuzzy grouping
    would be unsafe. Only strip an explicit trailing ``by`` credit when it looks
    like a creator list rather than part of a genuine title such as
    ``Batman by Gaslight``.
    """
    exact = _run_key(value)
    match = re.match(r"^(?P<title>.+?)\s+by\s+(?P<credit>.+)$", value, re.IGNORECASE)
    if match is None or not _looks_like_creator_credit(match.group("credit")):
        return (exact,)
    base = _run_key(match.group("title"))
    return (exact, base) if base and base != exact else (exact,)


def _looks_like_creator_credit(value: str) -> bool:
    parts = [part.strip() for part in re.split(r"\s*(?:&|\band\b)\s*", value) if part.strip()]
    if not parts:
        return False
    word_counts = [len(re.findall(r"[A-Za-z][A-Za-z'’-]*", part)) for part in parts]
    if len(parts) == 1:
        return word_counts[0] >= 2
    return all(count >= 1 for count in word_counts) and any(count >= 2 for count in word_counts)


def comic_run_context(candidate: NormalizedCandidate) -> NormalizedCandidate | None:
    """Project provider-derived issue run fields into a reusable run snapshot.

    No metadata is invented here: a context is returned only when Comic Vine
    supplied a run id, canonical series title, and explicit run start year on
    the issue candidate itself.
    """
    if (
        candidate.provider is not ProviderName.COMIC_VINE
        or candidate.record_type is not RecordType.COMIC_ISSUE
        or not candidate.run_id
        or not candidate.series_title
        or candidate.run_start_year is None
    ):
        return None
    return NormalizedCandidate(
        provider=ProviderName.COMIC_VINE,
        provider_id=candidate.run_id,
        record_type=RecordType.COMIC_RUN,
        media_kind=MediaKind.COMIC,
        title=candidate.series_title,
        identifiers=(Identifier("comic_vine", candidate.run_id),),
        publisher=candidate.publisher,
        series_title=candidate.series_title,
        run_start_year=candidate.run_start_year,
        run_id=candidate.run_id,
        provider_schema_version=candidate.provider_schema_version,
    )


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _run_key(left), _run_key(right)).ratio()
