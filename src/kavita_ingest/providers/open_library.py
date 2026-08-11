from __future__ import annotations

from typing import Any

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


class OpenLibraryProvider:
    name = ProviderName.OPEN_LIBRARY
    normalization_schema_version = 1
    endpoint = "https://openlibrary.org/search.json"

    def __init__(self, client: CachedProviderClient, contact: str | None) -> None:
        self.client = client
        self.contact = contact

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            self.name,
            True,
            False,
            bool(self.contact),
            "identified contact mode" if self.contact else "unidentified conservative mode",
            ("structured_search", "identifier_lookup", "exact_fetch", "work", "edition"),
        )

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        params = {"limit": "10", "fields": _FIELDS}
        if query.title:
            params["title"] = query.title
        if query.creators:
            params["author"] = query.creators[0]
        return self.client.get("search", self.endpoint, params, {}, "search", _normalize_search)

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        key = provider_id.strip("/")
        if key.startswith("OL") and key.endswith("M"):
            return self.client.get(
                "fetch-edition",
                f"https://openlibrary.org/books/{key}.json",
                {},
                {},
                "edition",
                _normalize_edition,
            )
        if key.startswith("works/"):
            key = key.split("/", 1)[1]
        return self.client.get(
            "fetch-work",
            f"https://openlibrary.org/works/{key}.json",
            {},
            {},
            "work",
            _normalize_work,
        )

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        scheme = identifier.scheme.casefold()
        if scheme not in {"isbn", "isbn10", "isbn13"}:
            return []
        return self.client.get(
            "isbn",
            f"https://openlibrary.org/isbn/{identifier.value}.json",
            {},
            {},
            "edition",
            _normalize_edition,
        )


_FIELDS = (
    "key,title,subtitle,author_name,first_publish_year,first_publish_date,"
    "edition_key,isbn,publisher,language"
)


def _normalize_search(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict) or not isinstance(raw.get("docs"), list):
        raise ValueError("search response requires a docs list")
    candidates: list[NormalizedCandidate] = []
    for doc in raw["docs"]:
        if not isinstance(doc, dict) or not isinstance(doc.get("title"), str):
            continue
        work_id = str(doc.get("key", "")).strip("/")
        if not work_id:
            continue
        creators = tuple(
            Contributor(str(name), "author") for name in _strings(doc.get("author_name"))
        )
        identifiers = tuple(Identifier("isbn", value) for value in _strings(doc.get("isbn")))
        common: dict[str, Any] = {
            "provider": ProviderName.OPEN_LIBRARY,
            "media_kind": MediaKind.BOOK,
            "title": doc["title"],
            "subtitle": _first_string(doc.get("subtitle")),
            "creators": creators,
            "identifiers": identifiers,
            "publisher": _first_string(doc.get("publisher")),
            "publication_date": _first_string(doc.get("first_publish_date"))
            or _year_string(doc.get("first_publish_year")),
            "language": _first_string(doc.get("language")),
            "work_id": work_id,
        }
        candidates.append(
            NormalizedCandidate(
                provider_id=work_id,
                record_type=RecordType.BOOK_WORK,
                **common,
            )
        )
    return candidates


def _normalize_work(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
        raise ValueError("work response requires a title")
    key = str(raw.get("key") or "").strip("/")
    if not key:
        raise ValueError("work response requires a key")
    return [
        NormalizedCandidate(
            ProviderName.OPEN_LIBRARY,
            key,
            RecordType.BOOK_WORK,
            MediaKind.BOOK,
            raw["title"],
            work_id=key,
            publication_date=_year_string(raw.get("first_publish_date")),
        )
    ]


def _normalize_edition(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
        raise ValueError("edition response requires a title")
    key = str(raw.get("key") or "").strip("/")
    edition_id = key.split("/", 1)[-1]
    if not edition_id:
        raise ValueError("edition response requires a key")
    identifiers: list[Identifier] = []
    for scheme in ("isbn_10", "isbn_13"):
        identifiers.extend(Identifier("isbn", value) for value in _strings(raw.get(scheme)))
    works = raw.get("works")
    work_id = None
    if isinstance(works, list) and works and isinstance(works[0], dict):
        work_id = str(works[0].get("key") or "").strip("/") or None
    languages = raw.get("languages")
    language = None
    if isinstance(languages, list) and languages and isinstance(languages[0], dict):
        language = str(languages[0].get("key") or "").rsplit("/", 1)[-1] or None
    return [
        NormalizedCandidate(
            ProviderName.OPEN_LIBRARY,
            edition_id,
            RecordType.BOOK_EDITION,
            MediaKind.BOOK,
            raw["title"],
            identifiers=tuple(identifiers),
            publisher=_first_string(raw.get("publishers")),
            publication_date=_first_string(raw.get("publish_date")),
            language=language,
            work_id=work_id,
            edition_id=edition_id,
        )
    ]


def _strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int))]
    if isinstance(value, str):
        return [value]
    return []


def _first_string(value: object) -> str | None:
    values = _strings(value)
    return values[0] if values else None


def _year_string(value: object) -> str | None:
    return str(value) if isinstance(value, int) else None
