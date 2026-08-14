from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from .collection_candidates import adapt_exact_collection_candidate
from .providers.base import Provider, ProviderError
from .providers.models import Contributor, Identifier, NormalizedCandidate

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HydrationResult:
    candidate: NormalizedCandidate
    status: str
    changes: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    error: str | None = None

    @property
    def accepted_detail(self) -> bool:
        return self.status == "hydrated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "changes": list(self.changes),
            "conflicts": list(self.conflicts),
            "error": self.error,
        }


def hydrate_candidate(
    selected: NormalizedCandidate, providers: tuple[Provider, ...]
) -> HydrationResult:
    provider = next((item for item in providers if item.name is selected.provider), None)
    if provider is None or "exact_fetch" not in provider.status().capabilities:
        return HydrationResult(selected, "unsupported")
    try:
        details = provider.fetch(selected.provider_id)
    except ProviderError as exc:
        LOGGER.debug(
            "exact hydration unavailable provider=%s id=%s",
            selected.provider,
            selected.provider_id,
        )
        return HydrationResult(selected, "unavailable", error=str(exc))
    exact = next(
        (
            candidate
            for candidate in details
            if candidate.provider is selected.provider
            and candidate.provider_id == selected.provider_id
        ),
        None,
    )
    if exact is None:
        return HydrationResult(
            selected,
            "unavailable",
            error="exact provider response did not contain the selected identity",
        )
    exact = adapt_exact_collection_candidate(selected, exact)
    return merge_exact_candidate(selected, exact)


def merge_exact_candidate(
    selected: NormalizedCandidate, exact: NormalizedCandidate
) -> HydrationResult:
    conflicts = _identity_conflicts(selected, exact)
    if conflicts:
        LOGGER.debug(
            "exact hydration conflict provider=%s id=%s fields=%s",
            selected.provider,
            selected.provider_id,
            len(conflicts),
        )
        return HydrationResult(selected, "conflict", conflicts=tuple(conflicts))

    creators = _contributors(selected.creators, exact.creators)
    identifiers = _identifiers(selected.identifiers, exact.identifiers)
    metadata = {**selected.provider_metadata, **exact.provider_metadata}
    hydrated = replace(
        selected,
        title=_detail_value(selected.title, exact.title) or selected.title,
        creators=creators,
        identifiers=identifiers,
        subtitle=_detail_value(selected.subtitle, exact.subtitle),
        publisher=_detail_value(selected.publisher, exact.publisher),
        publication_date=_detail_value(selected.publication_date, exact.publication_date),
        release_date=_detail_value(selected.release_date, exact.release_date),
        release_date_precision=_detail_value(
            selected.release_date_precision, exact.release_date_precision
        ),
        cover_date=_detail_value(selected.cover_date, exact.cover_date),
        cover_date_precision=_detail_value(
            selected.cover_date_precision, exact.cover_date_precision
        ),
        language=_detail_value(selected.language, exact.language),
        series_title=_detail_value(selected.series_title, exact.series_title),
        run_start_year=exact.run_start_year or selected.run_start_year,
        sequence=exact.sequence or selected.sequence,
        item_type=_detail_value(selected.item_type, exact.item_type),
        work_id=_detail_value(selected.work_id, exact.work_id),
        edition_id=_detail_value(selected.edition_id, exact.edition_id),
        run_id=_detail_value(selected.run_id, exact.run_id),
        provider_metadata=metadata,
        provider_schema_version=exact.provider_schema_version,
    )
    changes = _changes(selected, hydrated)
    LOGGER.debug(
        "exact hydration complete provider=%s id=%s changes=%s",
        selected.provider,
        selected.provider_id,
        ",".join(changes) or "none",
    )
    return HydrationResult(hydrated, "hydrated", changes=changes)


def _identity_conflicts(
    selected: NormalizedCandidate, exact: NormalizedCandidate
) -> list[str]:
    conflicts = []
    for label, left, right in (
        ("provider", selected.provider, exact.provider),
        ("provider ID", selected.provider_id, exact.provider_id),
        ("record type", selected.record_type, exact.record_type),
        ("media kind", selected.media_kind, exact.media_kind),
        ("issue number", selected.sequence.normalized if selected.sequence else None,
         exact.sequence.normalized if exact.sequence else None),
        ("run ID", selected.run_id, exact.run_id),
        ("series", _normalized(selected.series_title), _normalized(exact.series_title)),
        ("run start year", selected.run_start_year, exact.run_start_year),
        ("item type", selected.item_type, exact.item_type),
        ("release date", selected.release_date, exact.release_date),
        ("cover date", selected.cover_date, exact.cover_date),
    ):
        if left is not None and right is not None and left != right:
            conflicts.append(f"{label} differs: discovery={left!s}; exact={right!s}")
    if (
        selected.title
        and exact.title
        and _normalized(selected.title) != _normalized(exact.title)
        and _normalized(selected.title) != _normalized(selected.series_title)
    ):
        conflicts.append(
            f"title differs: discovery={selected.title!s}; exact={exact.title!s}"
        )
    return conflicts


def _detail_value[T](selected: T | None, exact: T | None) -> T | None:
    return exact if exact not in (None, "") else selected


def _contributors(
    selected: tuple[Contributor, ...], exact: tuple[Contributor, ...]
) -> tuple[Contributor, ...]:
    output = []
    seen: set[tuple[str, str]] = set()
    for contributor in (*selected, *exact):
        key = (contributor.name.casefold(), contributor.role.casefold())
        if key not in seen:
            seen.add(key)
            output.append(contributor)
    return tuple(output)


def _identifiers(
    selected: tuple[Identifier, ...], exact: tuple[Identifier, ...]
) -> tuple[Identifier, ...]:
    output = []
    seen: set[tuple[str, str]] = set()
    for identifier in (*selected, *exact):
        key = (identifier.scheme.casefold(), identifier.value)
        if key not in seen:
            seen.add(key)
            output.append(identifier)
    return tuple(output)


def _changes(
    selected: NormalizedCandidate, hydrated: NormalizedCandidate
) -> tuple[str, ...]:
    output = []
    if hydrated.creators != selected.creators:
        output.append("contributors")
    for field in (
        "title",
        "publisher",
        "release_date",
        "cover_date",
        "language",
        "identifiers",
    ):
        if getattr(hydrated, field) != getattr(selected, field):
            output.append(field)
    return tuple(output)


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
