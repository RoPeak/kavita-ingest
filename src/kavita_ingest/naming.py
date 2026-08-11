from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePath
from string import Formatter

from .domain import SequenceNumber

_INVALID = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_SPACES = re.compile(r"\s+")
_SEPARATORS = re.compile(r"(?:\s+-\s+){2,}")
_EMPTY_GROUPS = re.compile(r"\(\s*\)|\[\s*\]")
_TRAILING = re.compile(r"(?:\s*[-–—:,;]\s*)+$")
_ALLOWED_FIELDS = {
    "title",
    "series",
    "series_or_title",
    "number",
    "year",
    "author",
    "format",
}


@dataclass(frozen=True, slots=True)
class NamingPolicy:
    version: int = 1
    book_folder: str = "{series_or_title}"
    book_file: str = "{title}"
    book_series_file: str = "{series} - {number} - {title}"
    comic_folder: str = "{series}"
    comic_file: str = "{series} - {number} - {title}"
    integer_padding: int = 3
    comic_specials_subfolder: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def validate(self) -> None:
        if not 1 <= self.integer_padding <= 12:
            raise ValueError("sequence integer_padding must be between 1 and 12")
        for name, template in (
            ("book_folder", self.book_folder),
            ("book_file", self.book_file),
            ("book_series_file", self.book_series_file),
            ("comic_folder", self.comic_folder),
            ("comic_file", self.comic_file),
        ):
            validate_template(name, template)


def sanitize_component(value: str) -> str:
    cleaned = _SPACES.sub(" ", _INVALID.sub("_", value)).strip(" .")
    if cleaned in {"", ".", ".."}:
        raise ValueError("name component is empty after sanitization")
    return cleaned


def render_sequence(sequence: SequenceNumber | None, minimum_width: int = 3) -> str | None:
    return sequence.render(minimum_width) if sequence else None


def render_component(template: str, values: Mapping[str, object | None]) -> str:
    validate_template("naming", template)
    rendered = template.format_map(
        {key: "" if value is None else str(value) for key, value in values.items()}
    )
    rendered = _EMPTY_GROUPS.sub("", rendered)
    rendered = _SEPARATORS.sub(" - ", rendered)
    rendered = _TRAILING.sub("", rendered)
    rendered = re.sub(r"^\s*[-–—:,;]\s*", "", rendered)
    rendered = _SPACES.sub(" ", rendered).strip()
    return sanitize_component(rendered)


def validate_template(name: str, template: str) -> None:
    if not template.strip():
        raise ValueError(f"naming template {name} cannot be empty")
    fields = {
        field_name
        for _, field_name, format_spec, conversion in Formatter().parse(template)
        if field_name is not None
        and not format_spec
        and conversion is None
    }
    unknown = fields - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"naming template {name} has unsupported fields: {sorted(unknown)}")
    if any("/" in literal or "\\" in literal for literal, _, _, _ in Formatter().parse(template)):
        raise ValueError(f"naming template {name} must render one path component")


def join_optional(parts: list[str | None], separator: str = " - ") -> str:
    return separator.join(part for part in parts if part)


def detect_collisions(paths: list[PurePath]) -> dict[str, tuple[PurePath, ...]]:
    groups: dict[str, list[PurePath]] = {}
    for path in paths:
        key = "/".join(part.casefold() for part in path.parts)
        groups.setdefault(key, []).append(path)
    return {key: tuple(values) for key, values in groups.items() if len(values) > 1}
