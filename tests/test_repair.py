from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kavita_ingest.db import connect, migrate
from kavita_ingest.repair import reset_verified_publication


def _seed_completed_publication(database: Path, destination: Path) -> None:
    payload = (
        b'{"schema_version":3,"plan_id":"fixture","items":[],'
        b'"planning_policy":{},"conflicts":[]}'
    )
    digest = hashlib.sha256(payload).hexdigest()
    destination_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    with connect(database) as connection:
        plan_id = connection.execute(
            "INSERT INTO plans(schema_version, canonical_json, byte_length, sha256, status, "
            "created_at, approved_at, approval_digest) VALUES (3, ?, ?, ?, 'approved', ?, ?, ?)",
            (
                payload,
                len(payload),
                digest,
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:00+00:00",
                digest,
            ),
        ).lastrowid
        assert plan_id is not None
        connection.execute(
            "INSERT INTO apply_runs(id, plan_id, plan_digest, status, started_at, updated_at, "
            "completed_at) VALUES ('run', ?, ?, 'complete', ?, ?, ?)",
            (
                plan_id,
                digest,
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO apply_items(run_id, item_id, state, source_path, planned_source_hash, "
            "destination_path, destination_hash, lifecycle_policy, started_at, updated_at, "
            "completed_at) VALUES ('run', 'item', 'complete', '/old/source.cbr', ?, ?, ?, "
            "'move_after_verify', ?, ?, ?)",
            (
                "a" * 64,
                str(destination),
                destination_hash,
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:00+00:00",
            ),
        )
        connection.commit()


def test_reset_verified_publication_moves_only_matching_completed_destination(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    library = tmp_path / "library"
    incoming = tmp_path / "incoming"
    library.mkdir()
    incoming.mkdir()
    destination = library / "wrong.cbz"
    destination.write_bytes(b"verified-output")
    _seed_completed_publication(database, destination)

    target = incoming / "correct.cbz"
    with connect(database) as connection:
        result = reset_verified_publication(
            connection,
            destination,
            target,
            allowed_incoming_roots=(incoming,),
        )

    assert not destination.exists()
    assert target.read_bytes() == b"verified-output"
    assert result.incoming_path == target


def test_reset_verified_publication_refuses_changed_destination(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    library = tmp_path / "library"
    incoming = tmp_path / "incoming"
    library.mkdir()
    incoming.mkdir()
    destination = library / "wrong.cbz"
    destination.write_bytes(b"verified-output")
    _seed_completed_publication(database, destination)
    destination.write_bytes(b"changed-after-apply")

    with connect(database) as connection, pytest.raises(ValueError, match="no longer matches"):
        reset_verified_publication(
            connection,
            destination,
            incoming / "correct.cbz",
            allowed_incoming_roots=(incoming,),
        )

    assert destination.exists()
    assert not (incoming / "correct.cbz").exists()
