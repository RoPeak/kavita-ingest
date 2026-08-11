from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ..domain import MediaKind, SequenceNumber


class ProviderName(StrEnum):
    OPEN_LIBRARY = "open_library"
    GOOGLE_BOOKS = "google_books"
    COMIC_VINE = "comic_vine"


class RecordType(StrEnum):
    BOOK_WORK = "book_work"
    BOOK_EDITION = "book_edition"
    COMIC_RUN = "comic_run"
    COMIC_ISSUE = "comic_issue"
    COMIC_COLLECTION = "comic_collection"


@dataclass(frozen=True, slots=True)
class Identifier:
    scheme: str
    value: str


@dataclass(frozen=True, slots=True)
class Contributor:
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class NormalizedCandidate:
    provider: ProviderName
    provider_id: str
    record_type: RecordType
    media_kind: MediaKind
    title: str
    creators: tuple[Contributor, ...] = ()
    identifiers: tuple[Identifier, ...] = ()
    subtitle: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    language: str | None = None
    series_title: str | None = None
    run_start_year: int | None = None
    sequence: SequenceNumber | None = None
    item_type: str | None = None
    work_id: str | None = None
    edition_id: str | None = None
    run_id: str | None = None
    provider_schema_version: int = 1

    @property
    def key(self) -> str:
        return f"{self.provider.value}:{self.provider_id}:{self.record_type.value}"

    def data_hash(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NormalizedCandidate:
        sequence = value.get("sequence")
        return cls(
            provider=ProviderName(value["provider"]),
            provider_id=str(value["provider_id"]),
            record_type=RecordType(value["record_type"]),
            media_kind=MediaKind(value["media_kind"]),
            title=str(value["title"]),
            creators=tuple(Contributor(**item) for item in value.get("creators", [])),
            identifiers=tuple(Identifier(**item) for item in value.get("identifiers", [])),
            subtitle=value.get("subtitle"),
            publisher=value.get("publisher"),
            publication_date=value.get("publication_date"),
            language=value.get("language"),
            series_title=value.get("series_title"),
            run_start_year=value.get("run_start_year"),
            sequence=SequenceNumber.parse(sequence["raw"]) if sequence else None,
            item_type=value.get("item_type"),
            work_id=value.get("work_id"),
            edition_id=value.get("edition_id"),
            run_id=value.get("run_id"),
            provider_schema_version=int(value.get("provider_schema_version", 1)),
        )


@dataclass(frozen=True, slots=True)
class SearchQuery:
    media_kind: MediaKind
    title: str
    creators: tuple[str, ...] = ()
    identifiers: tuple[Identifier, ...] = ()
    series_title: str | None = None
    sequence: SequenceNumber | None = None
    run_start_year: int | None = None
    item_type: str | None = None
    provider_id: str | None = None
    relaxed: bool = False


def canonical_request_key(provider: ProviderName, operation: str, request: dict[str, Any]) -> str:
    return _digest({"provider": provider, "operation": operation, "request": request})


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
