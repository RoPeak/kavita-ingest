from __future__ import annotations

import errno
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .apply_journal import ItemState, RunState
from .filesystem import sha256_file


@dataclass(frozen=True, slots=True)
class PublishedReset:
    source_destination: Path
    incoming_path: Path
    sha256: str


def reset_verified_publication(
    connection: sqlite3.Connection,
    destination: Path,
    incoming_path: Path,
    *,
    allowed_incoming_roots: tuple[Path, ...],
) -> PublishedReset:
    """Move one verified completed destination back into Incoming for re-review.

    This is intentionally a reset, not an in-place metadata edit.  The historical
    apply journal remains immutable.  Only a destination whose current bytes still
    match a completed apply record can be moved, and the target must be inside a
    configured incoming root and must not already exist.
    """
    source = destination.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"published destination is not a regular file: {source}")

    target = incoming_path.expanduser().resolve(strict=False)
    if target.exists():
        raise ValueError(f"incoming reset target already exists: {target}")
    if not target.parent.is_dir():
        raise ValueError(f"incoming reset parent does not exist: {target.parent}")

    roots = tuple(root.expanduser().resolve(strict=False) for root in allowed_incoming_roots)
    if not any(target == root or target.is_relative_to(root) for root in roots):
        raise ValueError("incoming reset target must be inside a configured incoming root")

    row = connection.execute(
        "SELECT i.destination_hash FROM apply_items i "
        "JOIN apply_runs r ON r.id=i.run_id "
        "WHERE i.destination_path=? AND i.state=? AND r.status=? "
        "ORDER BY i.completed_at DESC LIMIT 1",
        (str(source), ItemState.COMPLETE.value, RunState.COMPLETE.value),
    ).fetchone()
    if row is None:
        raise ValueError("destination is not recorded as a completed verified publication")
    expected = str(row["destination_hash"] or "")
    if not expected:
        raise ValueError("completed publication has no durable destination hash")
    actual = sha256_file(source)
    if actual != expected:
        raise ValueError("published destination no longer matches its verified apply hash")

    if source.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError(
            "published reset requires source and incoming target on the same filesystem "
            "so the move is atomic"
        )
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise ValueError("published reset cannot cross filesystems") from exc
        raise
    return PublishedReset(source, target, actual)
