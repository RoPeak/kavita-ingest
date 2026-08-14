from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .domain import MediaKind, SequenceNumber


class ResolutionLevel(StrEnum):
    COMPLETE = "complete"
    WORK_ONLY = "work_only"
    MANUAL = "manual"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    media_kind: MediaKind
    title: str
    creators: tuple[str, ...]
    series_title: str | None = None
    sequence: SequenceNumber | None = None
    run_start_year: int | None = None
    item_type: str | None = None
    collection_volume: int | None = None
    publisher: str | None = None
    publication_date: str | None = None
    release_date: str | None = None
    release_date_precision: str | None = None
    cover_date: str | None = None
    cover_date_precision: str | None = None
    language: str | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    contributors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    description: str | None = None
    subjects: tuple[str, ...] = ()
    provider_identity: dict[str, str] = field(default_factory=dict)
    resolution: ResolutionLevel = ResolutionLevel.COMPLETE
    provenance: dict[str, str] = field(default_factory=dict)
    unresolved_fields: tuple[str, ...] = ()

    def planning_blocks(self) -> tuple[str, ...]:
        blocks = list(self.unresolved_fields)
        if not self.title and self.media_kind is MediaKind.BOOK:
            blocks.append("canonical title is unresolved")
        if not self.creators and self.media_kind is MediaKind.BOOK:
            blocks.append("book author/work creator is unresolved")
        if self.media_kind is MediaKind.COMIC:
            if not self.series_title:
                blocks.append("comic run/series identity is unresolved")
            if not self.item_type:
                blocks.append("comic item type is unresolved")
            elif self.item_type not in {
                "issue",
                "annual",
                "special",
                "one-shot",
                "trade",
                "collected-edition",
                "omnibus",
                "graphic-novel",
            }:
                blocks.append(f"comic item type is unsupported: {self.item_type}")
            if self.item_type in {"issue", "annual", "special"} and self.run_start_year is None:
                blocks.append("comic run start year is unresolved")
            if self.sequence is None and self.item_type in {"issue", "annual", "special"}:
                blocks.append("comic issue sequence is unresolved")
        if self.media_kind is MediaKind.UNKNOWN:
            blocks.append("media domain is unresolved")
        return tuple(dict.fromkeys(blocks))

    def to_dict(self) -> dict[str, Any]:
        sequence = None
        if self.sequence is not None:
            sequence = {
                "raw": self.sequence.raw,
                "normalized": self.sequence.normalized,
                "kind": self.sequence.kind.value,
                "sort_key": list(self.sequence.sort_key),
                "width": self.sequence.width,
            }
        return {
            "media_kind": self.media_kind.value,
            "title": self.title,
            "creators": list(self.creators),
            "series_title": self.series_title,
            "sequence": sequence,
            "run_start_year": self.run_start_year,
            "item_type": self.item_type,
            "collection_volume": self.collection_volume,
            "publisher": self.publisher,
            "publication_date": self.publication_date,
            "release_date": self.release_date,
            "release_date_precision": self.release_date_precision,
            "cover_date": self.cover_date,
            "cover_date_precision": self.cover_date_precision,
            "language": self.language,
            "identifiers": dict(sorted(self.identifiers.items())),
            "contributors": {key: list(value) for key, value in sorted(self.contributors.items())},
            "description": self.description,
            "subjects": list(self.subjects),
            "provider_identity": dict(sorted(self.provider_identity.items())),
            "resolution": self.resolution.value,
            "provenance": dict(sorted(self.provenance.items())),
            "unresolved_fields": list(self.unresolved_fields),
        }


def work_only_identity(
    *, title: str, creators: tuple[str, ...], series_title: str | None = None
) -> CanonicalIdentity:
    """Represent an explicitly accepted work without inventing edition facts."""
    return CanonicalIdentity(
        media_kind=MediaKind.BOOK,
        title=title,
        creators=creators,
        series_title=series_title,
        resolution=ResolutionLevel.WORK_ONLY,
        provenance={"title": "accepted_work", "creators": "accepted_work"},
    )
