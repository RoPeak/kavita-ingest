from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .canonical import CanonicalIdentity, ResolutionLevel
from .domain import MediaKind
from .naming import join_optional, render_sequence, sanitize_component


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


def project_comic(identity: CanonicalIdentity, extension: str = ".cbz") -> KavitaProjection:
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
    rendered = render_sequence(identity.sequence)
    base = join_optional([series, rendered, identity.title or None])
    folder = PurePosixPath(sanitize_component(series))
    if comic_format:
        folder /= "Specials"
    filename = sanitize_component(base) + _extension(extension)
    owned = OwnershipManifest(
        set_fields={key: value for key, value in metadata.items() if value != ""},
        clear_fields=tuple(key for key in ("Volume", "Format") if metadata[key] == ""),
        preserve_fields=("Notes", "Web", "PageCount", "Pages"),
    )
    return KavitaProjection(MediaKind.COMIC, metadata, filename, folder, owned)


def project_book(identity: CanonicalIdentity, extension: str = ".epub") -> KavitaProjection:
    if identity.media_kind is not MediaKind.BOOK:
        raise ValueError("book projection requires a book identity")
    blocks = identity.planning_blocks()
    if blocks:
        raise ValueError("; ".join(blocks))
    author = identity.creators[0]
    folder = PurePosixPath(sanitize_component(author))
    sequence = render_sequence(identity.sequence)
    if identity.series_title:
        folder /= sanitize_component(identity.series_title)
    filename = sanitize_component(join_optional([sequence, identity.title])) + _extension(extension)
    values: dict[str, Any] = {"title": identity.title, "authors": list(identity.creators)}
    if identity.series_title:
        values["series"] = identity.series_title
    if identity.sequence:
        values["series_index"] = identity.sequence.normalized
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
            values[field_name] = value
        else:
            unresolved.append(field_name)
    return KavitaProjection(
        MediaKind.BOOK,
        values,
        filename,
        folder,
        OwnershipManifest(
            set_fields=values,
            preserve_fields=tuple(preserve + ["manifest", "spine", "cover", "custom_metadata"]),
            unresolved_fields=tuple(unresolved),
        ),
    )


def _extension(value: str) -> str:
    return value if value.startswith(".") else f".{value}"
