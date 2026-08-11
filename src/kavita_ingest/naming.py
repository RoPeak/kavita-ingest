from __future__ import annotations

import re
from pathlib import PurePath

from .domain import SequenceNumber

_INVALID = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_SPACES = re.compile(r"\s+")


def sanitize_component(value: str) -> str:
    cleaned = _SPACES.sub(" ", _INVALID.sub("_", value)).strip(" .")
    if cleaned in {"", ".", ".."}:
        raise ValueError("name component is empty after sanitization")
    return cleaned


def render_sequence(sequence: SequenceNumber | None) -> str | None:
    return sequence.render(3) if sequence else None


def join_optional(parts: list[str | None], separator: str = " - ") -> str:
    return separator.join(part for part in parts if part)


def detect_collisions(paths: list[PurePath]) -> dict[str, tuple[PurePath, ...]]:
    groups: dict[str, list[PurePath]] = {}
    for path in paths:
        key = "/".join(part.casefold() for part in path.parts)
        groups.setdefault(key, []).append(path)
    return {key: tuple(values) for key, values in groups.items() if len(values) > 1}
