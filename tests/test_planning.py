from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel, work_only_identity
from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.planning import SourcePrecondition, build_snapshot, new_plan
from kavita_ingest.projection import project_book, project_comic


def _source(name: str, fingerprint: str) -> SourcePrecondition:
    return SourcePrecondition(
        f"/incoming/{name}", fingerprint * 64, 123, 456, Path(name).suffix[1:]
    )


def _manual_comic(item_id: str, name: str, number: str, fingerprint: str):  # type: ignore[no-untyped-def]
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        name,
        ("Alan Moore",),
        series_title="Watchmen",
        sequence=SequenceNumber.parse(number),
        run_start_year=1986,
        item_type="issue",
        resolution=ResolutionLevel.MANUAL,
        provenance={"title": "user", "series_title": "user", "sequence": "user"},
    )
    projection = project_comic(identity)
    return build_snapshot(
        item_id=item_id,
        source=_source(f"{item_id}.cbz", fingerprint),
        identity=identity,
        projection=projection,
        decision_provenance={
            "decision_type": "manual_identity",
            "decision_id": 7,
            "explicit_approval": True,
        },
        transformations=({"type": "metadata_only", "writer": "comicinfo_lxml"},),
        writer_versions={"comicinfo_schema": "2.1", "kavita_ingest": "0.1.0"},
        expected_inventory=({"name": "001.jpg", "sha256": "f" * 64},),
        verification_requirements=(
            "source_precondition",
            "zip_crc",
            "payload_hashes",
            "comicinfo_schema",
            "metadata_readback",
        ),
    )


def test_authoritative_plan_bytes_digest_approval_and_offline_export(tmp_path: Path) -> None:
    plan = new_plan("watchmen-plan", (_manual_comic("issue-1", "At Midnight", "1", "a"),))
    database = tmp_path / "state.sqlite3"
    migrate(database)
    export = tmp_path / "plan.json"
    with connect(database) as connection:
        store = PlanStore(connection)
        stored = store.add(plan)
        assert stored.canonical_json == plan.canonical_bytes()
        assert stored.byte_length == len(stored.canonical_json)
        assert stored.sha256 == hashlib.sha256(stored.canonical_json).hexdigest()
        with pytest.raises(ValueError, match="digest"):
            store.approve(stored.id, "0" * 64)
        approved = store.approve(stored.id, stored.sha256)
        assert approved.status == "approved" and approved.approval_digest == stored.sha256
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE plans SET canonical_json='{}' WHERE id=?", (stored.id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM plans WHERE id=?", (stored.id,))
        store.export(stored.id, export)
    assert export.read_bytes() == plan.canonical_bytes()
    assert b'provider_identity":{}' in export.read_bytes()
    assert b'"action":"move_after_verify"' in export.read_bytes()


def test_import_is_exact_and_always_creates_unapproved_draft(tmp_path: Path) -> None:
    plan = new_plan("exportable", (_manual_comic("issue-2", "The Judge", "2", "b"),))
    database = tmp_path / "state.sqlite3"
    migrate(database)
    with connect(database) as connection:
        store = PlanStore(connection)
        stored = store.import_bytes(plan.canonical_bytes())
        assert stored.status == "draft"
        altered = json.dumps(plan.to_dict(), indent=2).encode()
        with pytest.raises(ValueError, match="canonical"):
            store.import_bytes(altered)


def test_work_only_book_plan_writes_only_work_fields() -> None:
    identity = work_only_identity(title="A Wizard of Earthsea", creators=("Ursula K. Le Guin",))
    projection = project_book(identity)
    snapshot = build_snapshot(
        item_id="earthsea",
        source=_source("earthsea.epub", "c"),
        identity=identity,
        projection=projection,
        decision_provenance={
            "decision_type": "work_accepted",
            "decision_id": 8,
            "explicit_approval": True,
        },
        transformations=({"type": "metadata_only", "writer": "epub"},),
        writer_versions={"ebook-meta": "7.6.0", "opf_patcher": "1"},
        expected_inventory=(),
        verification_requirements=("publication_resource_hashes",),
    )
    assert snapshot.metadata_changes["set"] == {
        "title": "A Wizard of Earthsea",
        "authors": ["Ursula K. Le Guin"],
    }
    assert set(snapshot.metadata_changes["preserve"]) >= {
        "publisher",
        "date",
        "language",
        "identifiers",
    }


def test_unresolved_domain_run_and_item_type_are_precisely_blocked() -> None:
    unknown = CanonicalIdentity(MediaKind.UNKNOWN, "Mystery", ())
    blocked = build_snapshot(
        item_id="unknown",
        source=_source("unknown.pdf", "d"),
        identity=unknown,
        projection=None,
        decision_provenance={"explicit_approval": False},
        transformations=(),
        writer_versions={},
        expected_inventory=(),
        verification_requirements=(),
    )
    assert blocked.blocked
    assert [conflict.explanation for conflict in blocked.conflicts] == [
        "media domain is unresolved"
    ]


def test_plan_detects_casefolded_destination_collision_and_refuses_approval(tmp_path: Path) -> None:
    first = _manual_comic("one", "Same", "1", "e")
    second = _manual_comic("two", "Same", "1", "f")
    plan = new_plan("collision", (first, second))
    assert plan.conflicts[0].code == "destination_collision"
    database = tmp_path / "state.sqlite3"
    migrate(database)
    with connect(database) as connection:
        store = PlanStore(connection)
        stored = store.add(plan)
        with pytest.raises(ValueError, match="conflicts"):
            store.approve(stored.id, stored.sha256)


def test_plan_collision_identity_uses_frozen_absolute_destination() -> None:
    first = _manual_comic("one", "Same", "1", "e")
    second = _manual_comic("two", "Same", "1", "f")

    def rooted(item: object, root: str):  # type: ignore[no-untyped-def]
        projection = dict(item.kavita_projection)  # type: ignore[attr-defined]
        projection["library_root"] = root
        projection["absolute_destination"] = str(Path(root) / projection["destination"])
        return replace(item, kavita_projection=projection)

    separate = new_plan("separate", (rooted(first, "/books"), rooted(second, "/comics")))
    assert not separate.conflicts

    same = new_plan("same", (rooted(first, "/library"), rooted(second, "/LIBRARY")))
    assert same.conflicts[0].code == "destination_collision"


def test_projected_item_without_explicit_item_decision_is_rejected(tmp_path: Path) -> None:
    item = _manual_comic("implicit", "No", "3", "9").to_dict()
    item["provenance"]["explicit_approval"] = False
    document = {
        "schema_version": 1,
        "plan_id": "implicit",
        "created_at": "2026-01-01T00:00:00+00:00",
        "items": [item],
        "conflicts": [],
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    database = tmp_path / "state.sqlite3"
    migrate(database)
    with connect(database) as connection, pytest.raises(ValueError, match="explicit approval"):
        PlanStore(connection).import_bytes(payload)
