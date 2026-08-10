from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class SequenceNumber:
    raw: str

    @property
    def metadata(self) -> str:
        if re.fullmatch(r"\d+", self.raw):
            return str(int(self.raw))
        if re.fullmatch(r"\d+\.\d+", self.raw):
            whole, fraction = self.raw.split(".", 1)
            return f"{int(whole)}.{fraction}"
        return self.raw

    def filename(self, width: int = 3) -> str:
        if re.fullmatch(r"\d+", self.raw):
            return f"{int(self.raw):0{width}d}"
        return self.metadata

    def integer_value(self) -> int | None:
        return int(self.raw) if re.fullmatch(r"\d+", self.raw) else None


@dataclass(frozen=True)
class CanonicalComic:
    series_title: str
    run_start_year: int | None
    kind: str
    sequence: SequenceNumber | None = None
    collection_sequence: SequenceNumber | None = None
    title: str | None = None


@dataclass(frozen=True)
class KavitaProjection:
    series: str
    number: str | None
    volume: int | None
    format: str | None
    filename: str
    folder: PurePosixPath


def _suffix(title: str | None) -> str:
    return f" - {title}" if title else ""


def project(comic: CanonicalComic) -> KavitaProjection:
    series = (
        f"{comic.series_title} ({comic.run_start_year})"
        if comic.run_start_year is not None
        else comic.series_title
    )
    number: str | None = None
    volume: int | None = None
    format_value: str | None = None

    if comic.kind == "issue":
        if comic.sequence is None:
            raise ValueError("issue sequence required")
        number = comic.sequence.metadata
        stem = f"{series} #{comic.sequence.filename()}"
    elif comic.kind == "annual":
        sequence = comic.sequence or SequenceNumber("1")
        number = sequence.metadata
        format_value = "Annual"
        stem = f"{series} Annual {sequence.filename(2)}"
    elif comic.kind in {"special", "one_shot"}:
        sequence = comic.sequence or SequenceNumber("1")
        number = sequence.metadata
        format_value = "One-Shot" if comic.kind == "one_shot" else "Special"
        stem = f"{series} SP{sequence.filename(2)}"
    elif comic.kind in {"trade", "omnibus"}:
        if comic.collection_sequence is None:
            raise ValueError("collection sequence required")
        volume = comic.collection_sequence.integer_value()
        format_value = "TPB" if comic.kind == "trade" else "Omnibus"
        if volume is None:
            number = comic.collection_sequence.metadata
            stem = f"{series} SP{comic.collection_sequence.filename(2)}"
        else:
            stem = f"{series} - v{comic.collection_sequence.filename(2)}"
    elif comic.kind == "graphic_novel":
        format_value = "Graphic Novel"
        stem = f"{series} SP01"
    else:
        raise ValueError(f"unsupported canonical comic kind: {comic.kind}")

    filename = f"{stem}{_suffix(comic.title)}.cbz"
    return KavitaProjection(
        series=series,
        number=number,
        volume=volume,
        format=format_value,
        filename=filename,
        folder=PurePosixPath("Comics", series),
    )
