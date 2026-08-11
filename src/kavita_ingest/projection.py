from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from .canonical import CanonicalIdentity, ResolutionLevel
from .domain import MediaKind
from .naming import NamingPolicy, render_component, render_sequence


@dataclass(frozen=True, slots=True)
class OwnershipManifest:
    set_fields: dict[str, Any] = field(default_factory=dict)
    clear_fields: tuple[str, ...] = ()
    preserve_fields: tuple[str, ...] = ()
    unresolved_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        buckets = (
            set(self.set_fields),
            set(self.clear_fields),
            set(self.preserve_fields),
            set(self.unresolved_fields),
        )
        for index, left in enumerate(buckets):
            for right in buckets[index + 1 :]:
                overlap = left & right
                if overlap:
                    raise ValueError(f"metadata ownership categories overlap: {sorted(overlap)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "set": self.set_fields,
            "clear": list(self.clear_fields),
            "preserve": list(self.preserve_fields),
            "unresolved": list(self.unresolved_fields),
        }


@dataclass(frozen=True, slots=True)
class KavitaProjection:
    media_kind: MediaKind
    metadata: dict[str, Any]
    filename: str
    destination_folder: PurePosixPath
    ownership: OwnershipManifest

    @property
    def destination(self) -> PurePosixPath:
        return self.destination_folder / self.filename

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_kind": self.media_kind.value,
            "metadata": self.metadata,
            "filename": self.filename,
            "destination_folder": self.destination_folder.as_posix(),
            "destination": self.destination.as_posix(),
            "ownership": self.ownership.to_dict(),
        }


_FORMATS = {
    "annual": "Annual",
    "special": "Special",
    "one-shot": "One-Shot",
    "trade": "Trade Paperback",
    "collected-edition": "Trade Paperback",
    "omnibus": "Omnibus",
    "graphic-novel": "Graphic Novel",
}

_COMICINFO_CONTRIBUTORS = {
    "writers": "Writer",
    "pencillers": "Penciller",
    "inkers": "Inker",
    "colorists": "Colorist",
    "letterers": "Letterer",
    "cover_artists": "CoverArtist",
    "editors": "Editor",
    "translators": "Translator",
}


def project_comic(
    identity: CanonicalIdentity,
    extension: str = ".cbz",
    naming: NamingPolicy | None = None,
) -> KavitaProjection:
    if identity.media_kind is not MediaKind.COMIC:
        raise ValueError("comic projection requires a comic identity")
    blocks = identity.planning_blocks()
    if blocks:
        raise ValueError("; ".join(blocks))
    assert identity.series_title is not None
    series = identity.series_title
    if identity.run_start_year is not None:
        series = f"{series} ({identity.run_start_year})"
    number = identity.sequence.normalized if identity.sequence else "1"
    item_type = identity.item_type or "issue"
    comic_format = _FORMATS.get(item_type, "")
    volume = identity.collection_volume if identity.collection_volume is not None else ""
    metadata: dict[str, Any] = {
        "Series": series,
        "Number": number,
        "Volume": volume,
        "Format": comic_format,
        "Title": identity.title,
    }
    for group, field_name in _COMICINFO_CONTRIBUTORS.items():
        names = identity.contributors.get(group, ())
        if names:
            metadata[field_name] = ", ".join(names)
    if identity.publisher:
        metadata["Publisher"] = identity.publisher
    metadata.update(_comic_date_fields(identity.publication_date))
    if identity.language:
        metadata["LanguageISO"] = identity.language
    policy = naming or NamingPolicy()
    policy.validate()
    naming_values = {
        "title": identity.title or None,
        "series": series,
        "series_or_title": series,
        "number": render_sequence(identity.sequence, policy.integer_padding),
        "year": identity.run_start_year,
        "author": identity.creators[0] if identity.creators else None,
        "format": comic_format or None,
    }
    folder = PurePosixPath(render_component(policy.comic_folder, naming_values))
    if comic_format and policy.comic_specials_subfolder:
        folder /= "Specials"
    filename = render_component(policy.comic_file, naming_values) + _extension(extension)
    owned = OwnershipManifest(
        set_fields={key: value for key, value in metadata.items() if value != ""},
        clear_fields=tuple(key for key in ("Volume", "Format") if metadata[key] == ""),
        preserve_fields=("Notes", "Web", "PageCount", "Pages"),
    )
    return KavitaProjection(MediaKind.COMIC, metadata, filename, folder, owned)


def project_book(
    identity: CanonicalIdentity,
    extension: str = ".epub",
    naming: NamingPolicy | None = None,
) -> KavitaProjection:
    if identity.media_kind is not MediaKind.BOOK:
        raise ValueError("book projection requires a book identity")
    blocks = identity.planning_blocks()
    if blocks:
        raise ValueError("; ".join(blocks))
    policy = naming or NamingPolicy()
    policy.validate()
    series_or_title = identity.series_title or identity.title
    naming_values = {
        "title": identity.title,
        "series": identity.series_title,
        "series_or_title": series_or_title,
        "number": render_sequence(identity.sequence, policy.integer_padding),
        "year": identity.publication_date[:4] if identity.publication_date else None,
        "author": identity.creators[0],
        "format": None,
    }
    folder = PurePosixPath(render_component(policy.book_folder, naming_values))
    template = policy.book_series_file if identity.series_title else policy.book_file
    filename = render_component(template, naming_values) + _extension(extension)
    metadata_values: dict[str, Any] = {
        "title": identity.title,
        "authors": list(identity.creators),
    }
    if identity.series_title:
        metadata_values["series"] = identity.series_title
    if identity.sequence:
        metadata_values["series_index"] = identity.sequence.normalized
    edition_values = {
        "publisher": identity.publisher,
        "date": identity.publication_date,
        "language": identity.language,
        "identifiers": identity.identifiers or None,
    }
    unresolved: list[str] = []
    preserve: list[str] = []
    for field_name, value in edition_values.items():
        if identity.resolution is ResolutionLevel.WORK_ONLY:
            preserve.append(field_name)
        elif value is not None:
            metadata_values[field_name] = value
        else:
            unresolved.append(field_name)
    return KavitaProjection(
        MediaKind.BOOK,
        metadata_values,
        filename,
        folder,
        OwnershipManifest(
            set_fields=metadata_values,
            preserve_fields=tuple(preserve + ["manifest", "spine", "cover", "custom_metadata"]),
            unresolved_fields=tuple(unresolved),
        ),
    )


def _extension(value: str) -> str:
    return value if value.startswith(".") else f".{value}"


def _comic_date_fields(value: str | None) -> dict[str, int]:
    if not value:
        return {}
    if match := re.fullmatch(r"(\d{4})", value):
        year = int(match.group(1))
        return {"Year": year} if year > 0 else {}
    if match := re.fullmatch(r"(\d{4})-(\d{2})", value):
        year, month = (int(part) for part in match.groups())
        return {"Year": year, "Month": month} if year > 0 and 1 <= month <= 12 else {}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return {}
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return {}
    return {"Year": parsed.year, "Month": parsed.month, "Day": parsed.day}
