from __future__ import annotations

from dataclasses import dataclass

from .matching import LocalIdentity
from .providers.base import Provider, ProviderError
from .providers.models import NormalizedCandidate, ProviderName, SearchQuery


@dataclass(frozen=True, slots=True)
class CandidateGeneration:
    candidates: tuple[NormalizedCandidate, ...]
    queries: tuple[str, ...]
    unavailable: tuple[str, ...]


def generate_candidates(
    local: LocalIdentity, providers: tuple[Provider, ...]
) -> CandidateGeneration:
    output: dict[str, NormalizedCandidate] = {}
    queries: list[str] = []
    unavailable: list[str] = []
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
                continue
            strong = SearchQuery(
                media_kind=local.kind,
                title=local.title,
                creators=local.creators,
                identifiers=local.identifiers,
                series_title=local.series_title,
                sequence=local.sequence,
                run_start_year=local.run_start_year,
                item_type=local.subtype,
            )
            queries.append(f"{status.provider.value}:structured")
            _merge(output, provider.search(strong))
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
    return CandidateGeneration(tuple(output.values()), tuple(queries), tuple(unavailable))


def _merge(destination: dict[str, NormalizedCandidate], values: list[NormalizedCandidate]) -> None:
    for item in values:
        destination[item.key] = item
