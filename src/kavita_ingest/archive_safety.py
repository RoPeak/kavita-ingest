from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


class ArchiveMember(Protocol):
    filename: str
    file_size: int
    compress_size: int

    def is_dir(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = 5_000
    max_entry_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_path_depth: int = 20
    max_ratio: float = 1_000.0


class UnsafeArchive(ValueError):
    pass


def validate_inventory(
    members: Sequence[ArchiveMember],
    limits: ArchiveLimits,
    *,
    link_names: set[str] | None = None,
    encrypted_names: set[str] | None = None,
) -> None:
    if len(members) > limits.max_entries:
        raise UnsafeArchive(f"archive has {len(members)} entries; limit is {limits.max_entries}")
    links = link_names or set()
    encrypted = encrypted_names or set()
    seen: set[str] = set()
    total = 0
    for member in members:
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in path.parts:
            raise UnsafeArchive(f"unsafe archive path: {member.filename!r}")
        if len(path.parts) > limits.max_path_depth:
            raise UnsafeArchive(f"archive path is too deep: {member.filename!r}")
        folded = name.casefold().rstrip("/")
        if folded in seen:
            raise UnsafeArchive(f"duplicate or case-colliding archive path: {member.filename!r}")
        seen.add(folded)
        if name in links:
            raise UnsafeArchive(f"archive links are unsupported: {member.filename!r}")
        if name in encrypted:
            raise UnsafeArchive(f"encrypted archive member: {member.filename!r}")
        if member.is_dir():
            continue
        if member.file_size > limits.max_entry_bytes:
            raise UnsafeArchive(f"archive member exceeds size limit: {member.filename!r}")
        total += member.file_size
        if total > limits.max_total_bytes:
            raise UnsafeArchive("archive exceeds aggregate uncompressed size limit")
        if member.compress_size > 0 and member.file_size / member.compress_size > limits.max_ratio:
            raise UnsafeArchive(
                f"archive member exceeds compression-ratio limit: {member.filename!r}"
            )
