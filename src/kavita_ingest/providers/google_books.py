from __future__ import annotations

from ..domain import MediaKind
from .base import ProviderStatus
from .client import CachedProviderClient
from .models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)


class GoogleBooksProvider:
    name = ProviderName.GOOGLE_BOOKS
    normalization_schema_version = 1
    endpoint = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, client: CachedProviderClient, api_key: str | None = None) -> None:
        self.client = client
        self.api_key = api_key

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            self.name,
            True,
            False,
            bool(self.api_key),
            "API key configured" if self.api_key else "anonymous access; quota may be lower",
            ("structured_search", "identifier_lookup", "exact_fetch", "edition"),
        )

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        parts = [f'"{query.title}"'] if query.relaxed else [f'intitle:"{query.title}"']
        if query.creators:
            parts.append(f'inauthor:"{query.creators[0]}"')
        return self._request("search", {"q": " ".join(parts), "maxResults": "10"})

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        url = f"{self.endpoint}/{provider_id}"
        return self.client.get("fetch", url, {}, self._secret(), "volumes", _normalize_single)

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        if identifier.scheme.casefold() not in {"isbn", "isbn10", "isbn13"}:
            return []
        return self._request("isbn", {"q": f"isbn:{identifier.value}", "maxResults": "10"})

    def _request(self, operation: str, params: dict[str, str]) -> list[NormalizedCandidate]:
        return self.client.get(
            operation, self.endpoint, params, self._secret(), "volumes", _normalize_search
        )

    def _secret(self) -> dict[str, str]:
        return {"key": self.api_key} if self.api_key else {}


def _normalize_search(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict):
        raise ValueError("volume response must be an object")
    items = raw.get("items", [])
    if not isinstance(items, list):
        raise ValueError("volume items must be a list")
    return [candidate for item in items if (candidate := _normalize_item(item)) is not None]


def _normalize_single(raw: object) -> list[NormalizedCandidate]:
    candidate = _normalize_item(raw)
    return [candidate] if candidate else []


def _normalize_item(raw: object) -> NormalizedCandidate | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    info = raw.get("volumeInfo")
    if not isinstance(info, dict) or not isinstance(info.get("title"), str):
        return None
    identifiers = []
    raw_identifiers = info.get("industryIdentifiers", [])
    if not isinstance(raw_identifiers, list):
        raise ValueError("industryIdentifiers must be a list")
    for item in raw_identifiers:
        if isinstance(item, dict) and item.get("type") and item.get("identifier"):
            identifiers.append(Identifier(str(item["type"]).casefold(), str(item["identifier"])))
    return NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        raw["id"],
        RecordType.BOOK_EDITION,
        MediaKind.BOOK,
        info["title"],
        creators=tuple(
            Contributor(str(author), "author")
            for author in info.get("authors", [])
            if isinstance(author, str)
        ),
        identifiers=tuple(identifiers),
        subtitle=str(info["subtitle"]) if info.get("subtitle") else None,
        publisher=str(info["publisher"]) if info.get("publisher") else None,
        publication_date=str(info["publishedDate"]) if info.get("publishedDate") else None,
        language=str(info["language"]) if info.get("language") else None,
        edition_id=raw["id"],
    )
