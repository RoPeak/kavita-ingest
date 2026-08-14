from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

from .domain import SourceFormat, SourceRecord

SUPPORTED_EXTENSIONS = {".epub", ".pdf", ".cbz", ".cbr", ".rar"}
_VOLUME_PATTERNS = (
    re.compile(r"\.part0*\d+\.rar$", re.IGNORECASE),
    re.compile(r"\.r\d\d$", re.IGNORECASE),
)


def discover(root: Path, excluded_roots: tuple[Path, ...] = ()) -> Iterator[Path]:
    root = root.expanduser().resolve(strict=True)
    excluded = tuple(path.expanduser().resolve(strict=False) for path in excluded_roots)
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not _is_excluded(current_path / name, excluded)
            and not (current_path / name).is_symlink()
        )
        for name in sorted(files, key=_natural_name_key):
            path = current_path / name
            if path.is_symlink() or _is_excluded(path, excluded):
                continue
            if path.suffix.casefold() in SUPPORTED_EXTENSIONS or is_multivolume_name(path.name):
                yield path


def inspect_source(path: Path) -> SourceRecord:
    before = path.stat()
    signature, detected = detect_signature(path)
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("source changed while it was being fingerprinted")
    return SourceRecord(
        path=path.resolve(),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        sha256=digest,
        format=detected,
        signature=signature,
    )


def detect_signature(path: Path) -> tuple[str, SourceFormat]:
    with path.open("rb") as handle:
        header = handle.read(16)
    suffix = path.suffix.casefold()
    if header.startswith(b"%PDF-"):
        return "pdf", SourceFormat.PDF
    if header.startswith(b"Rar!\x1a\x07\x00") or header.startswith(b"Rar!\x1a\x07\x01\x00"):
        return "rar", SourceFormat.CBR
    if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
        return ("zip-epub", SourceFormat.EPUB) if suffix == ".epub" else ("zip", SourceFormat.CBZ)
    return "unknown", SourceFormat.UNKNOWN


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_multivolume_name(name: str) -> bool:
    return any(pattern.search(name) for pattern in _VOLUME_PATTERNS)


def is_multivolume_path(path: Path) -> bool:
    if is_multivolume_name(path.name):
        return True
    if path.suffix.casefold() in {".rar", ".cbr"}:
        stem = path.with_suffix("")
        return any(stem.with_suffix(f".r{index:02d}").exists() for index in range(100))
    return False


def failed_source_record(path: Path) -> SourceRecord:
    try:
        stat = path.stat()
        size, mtime_ns = stat.st_size, stat.st_mtime_ns
    except OSError:
        size, mtime_ns = 0, 0
    return SourceRecord(
        path=path.resolve(strict=False),
        size=size,
        mtime_ns=mtime_ns,
        sha256="",
        format=SourceFormat.UNKNOWN,
        signature="unreadable",
    )


def _natural_name_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Sort numbered media in human sequence without changing filenames."""
    parts = re.split(r"(\d+)", value.casefold())
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
        if part
    )


def _is_excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved == root or root in resolved.parents for root in excluded)
