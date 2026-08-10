from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Identifier, NormalizedCandidate, ProviderName, SearchQuery


class ProviderError(RuntimeError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class MalformedProviderResponse(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: ProviderName
    enabled: bool
    credential_required: bool
    credential_present: bool
    detail: str
    capabilities: tuple[str, ...]


class Provider(Protocol):
    name: ProviderName

    def status(self) -> ProviderStatus: ...

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]: ...

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]: ...

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]: ...
