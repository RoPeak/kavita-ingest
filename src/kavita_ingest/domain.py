from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class SequenceKind(StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    ALPHANUMERIC = "alphanumeric"
    RANGE = "range"
    SYMBOLIC = "symbolic"


@dataclass(frozen=True, slots=True)
class SequenceNumber:
    raw: str
    normalized: str
    kind: SequenceKind
    sort_key: tuple[int, int, str]
    width: int | None = None

    @classmethod
    def parse(cls, value: str) -> SequenceNumber:
        raw = value.strip()
        if not raw:
            raise ValueError("sequence number cannot be empty")
        if match := re.fullmatch(r"(\d+)", raw):
            number = int(match.group(1))
            return cls(raw, str(number), SequenceKind.INTEGER, (0, number, ""), len(raw))
        if match := re.fullmatch(r"(\d+)\.(\d+)", raw):
            whole, fraction = match.groups()
            normalized = f"{int(whole)}.{fraction.rstrip('0') or '0'}"
            fraction_sort = (fraction.rstrip("0") or "0").ljust(18, "0")
            return cls(raw, normalized, SequenceKind.DECIMAL, (1, int(whole), fraction_sort))
        if match := re.fullmatch(r"(\d+)([A-Za-z]+)", raw):
            number_text, suffix = match.groups()
            normalized = f"{int(number_text)}{suffix.upper()}"
            return cls(
                raw,
                normalized,
                SequenceKind.ALPHANUMERIC,
                (2, int(number_text), suffix.casefold()),
            )
        if match := re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw):
            start, end = (int(part) for part in match.groups())
            return cls(raw, f"{start}-{end}", SequenceKind.RANGE, (3, start, f"{end:09d}"))
        normalized = re.sub(r"\s+", " ", raw).upper()
        return cls(raw, normalized, SequenceKind.SYMBOLIC, (4, 0, normalized.casefold()))

    def render(self, minimum_width: int = 3) -> str:
        if self.kind is SequenceKind.INTEGER:
            return f"{int(self.normalized):0{max(minimum_width, self.width or 0)}d}"
        return self.normalized


class MediaKind(StrEnum):
    BOOK = "book"
    COMIC = "comic"
    UNKNOWN = "unknown"


class SourceFormat(StrEnum):
    EPUB = "epub"
    PDF = "pdf"
    CBZ = "cbz"
    CBR = "cbr"
    UNKNOWN = "unknown"


class InspectionStatus(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Evidence:
    field: str
    raw: str
    normalized: str
    source: str
    confidence: float
    is_noise: bool = False
    span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class ParseHypothesis:
    kind: MediaKind
    subtype: str
    confidence: float
    title: str | None = None
    series: str | None = None
    sequence: SequenceNumber | None = None
    year: int | None = None
    creators: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Classification:
    kind: MediaKind
    subtype: str
    confidence: float
    ambiguous: bool
    hypotheses: tuple[ParseHypothesis, ...]


@dataclass(frozen=True, slots=True)
class SourceRecord:
    path: Path
    size: int
    mtime_ns: int
    sha256: str
    format: SourceFormat
    signature: str


@dataclass(frozen=True, slots=True)
class InspectionResult:
    status: InspectionStatus
    format: SourceFormat
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
