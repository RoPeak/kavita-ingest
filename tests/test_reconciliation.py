from __future__ import annotations

import gc
import json
import os
from pathlib import Path

import pytest

from compatibility.helpers.epub_factory import create_epub
from kavita_ingest.config import AppConfig
from kavita_ingest.db import migrate
from kavita_ingest.reconciliation import (
    ReconciliationRefused,
    approve_plan,
    compare_publications,
    create_plan,
    create_state_template,
    execute,
    preview,
    sha256_file,
)


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    archive = tmp_path / "archive"
    state = tmp_path / "state"
    series = books / "Dungeon Crawler Carl"
    for path in (incoming, books, archive, state / "kavita-reports", series):
        path.mkdir(parents=True, exist_ok=True)
    source = create_epub(
        incoming / "Dungeon Crawler Carl.epub",
        version="2.0",
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        embedded_series=None,
    )
    old = create_epub(
        series / "Dungeon Crawler Carl.epub",
        version="2.0",
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        embedded_series=None,
    )
    new = create_epub(
        series / "Dungeon Crawler Carl - 001 - Dungeon Crawler Carl.epub",
        version="2.0",
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        embedded_series=("Dungeon Crawler Carl", "1"),
    )
    database = state / "state.sqlite3"
    migrate(database)
    config = AppConfig(
        incoming_roots=(incoming,),
        books_root=books,
        comics_root=tmp_path / "comics",
        database_path=database,
    )
    report = {
        "report_schema": "kavita-ingest-publication-v3",
        "status": "completed",
        "recovery_required": False,
        "approved_apply": True,
        "source_lifecycle": "preserved",
        "verification_completed": True,
        "items": [
            {
                "item_state": "complete",
                "cleanup_eligible": True,
                "source_lifecycle": "preserved",
                "action": "published",
                "source": str(source),
                "source_sha256": sha256_file(source),
                "destination": str(destination),
                "destination_sha256": sha256_file(destination),
            }
            for destination in (old, new)
        ],
    }
    (state / "kavita-reports" / "publications.json").write_text(json.dumps(report))
    return config, state, archive, source, old, new


def _plan(tmp_path: Path):  # type: ignore[no-untyped-def]
    config, state, archive, source, old, new = _fixture(tmp_path)
    plan_path = tmp_path / "reconciliation.json"
    plan, digest = create_plan(
        [(old, new)], archive, plan_path, config, state_root=state
    )
    return config, state, archive, source, old, new, plan_path, plan, digest


def _approve(plan_path: Path, plan: dict[str, object], digest: str, tmp_path: Path) -> Path:
    state_report = tmp_path / "kavita-state.json"
    create_state_template(plan_path, state_report)
    payload = json.loads(state_report.read_text())
    payload["reviewed_at"] = "2026-11-10T12:00:00+00:00"
    for item in payload["items"]:
        item.update(
            {
                "old_kavita_id": 10,
                "new_kavita_id": 20,
                "reading_progress": "none",
                "custom_cover": "none",
                "resolution_note": "checked through supported UI/API",
            }
        )
    state_report.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(state_report, 0o600)
    return approve_plan(plan_path, digest, state_report)


def test_metadata_only_epub_rewrite_is_safe_content_match(tmp_path: Path) -> None:
    config, _, _, _, old, new = _fixture(tmp_path)

    result = compare_publications(old, new, config)

    assert result["old"]["sha256"] != result["new"]["sha256"]
    assert result["comparison"] == {
        "exact_bytes": False,
        "same_inode": False,
        "title_matches": True,
        "creators_match": True,
        "resources_match": True,
        "package_structure_matches": True,
        "outcome": "metadata-only-rewrite",
        "archivable": True,
    }
    assert result["new"]["series"] == "Dungeon Crawler Carl"
    assert result["new"]["series_index"] == "1"


def test_repeated_epub_comparison_does_not_accumulate_descriptors(tmp_path: Path) -> None:
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("descriptor regression test requires procfs")
    config, _, _, _, old, new = _fixture(tmp_path)
    before = len(list(Path("/proc/self/fd").iterdir()))
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(40):
            compare_publications(old, new, config)
        after = len(list(Path("/proc/self/fd").iterdir()))
    finally:
        if enabled:
            gc.enable()

    assert after <= before + 2


def test_different_publication_content_requires_manual_review(tmp_path: Path) -> None:
    config, _, _, _, old, new = _fixture(tmp_path)
    data = new.read_bytes().replace(b"Publication content.", b"Abridged publication.")
    new.write_bytes(data)

    result = compare_publications(old, new, config)

    assert result["comparison"]["archivable"] is False
    assert result["comparison"]["outcome"] in {
        "invalid-epub-manual-review",
        "publication-content-differs-manual-review",
    }


def test_unknown_kavita_progress_or_cover_blocks_approval(tmp_path: Path) -> None:
    _, _, _, _, _, _, plan_path, _, digest = _plan(tmp_path)
    state_report = tmp_path / "state-review.json"
    create_state_template(plan_path, state_report)
    payload = json.loads(state_report.read_text())
    payload["reviewed_at"] = "2026-11-10T12:00:00+00:00"
    state_report.write_text(json.dumps(payload))

    with pytest.raises(ReconciliationRefused, match="progress/cover review"):
        approve_plan(plan_path, digest, state_report)


@pytest.mark.parametrize(
    ("field", "value"),
    [("reading_progress", "unknown"), ("custom_cover", "unknown")],
)
def test_each_kavita_state_risk_independently_blocks_approval(
    tmp_path: Path, field: str, value: str
) -> None:
    _, _, _, _, _, _, plan_path, plan, digest = _plan(tmp_path)
    state_report = tmp_path / "state-review.json"
    create_state_template(plan_path, state_report)
    payload = json.loads(state_report.read_text())
    payload["reviewed_at"] = "2026-11-10T12:00:00+00:00"
    for item in payload["items"]:
        item.update(
            {
                "old_kavita_id": 10,
                "new_kavita_id": 20,
                "reading_progress": "none",
                "custom_cover": "none",
                "resolution_note": "reviewed",
            }
        )
        item[field] = value
    state_report.write_text(json.dumps(payload))

    with pytest.raises(ReconciliationRefused, match="progress/cover review"):
        approve_plan(plan_path, digest, state_report)


@pytest.mark.parametrize(
    "updates",
    [
        {"old_kavita_id": None},
        {"new_kavita_id": None},
        {"old_kavita_id": 10, "new_kavita_id": 10},
        {"resolution_note": ""},
    ],
)
def test_incomplete_kavita_identity_review_blocks_approval(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    _, _, _, _, _, _, plan_path, plan, digest = _plan(tmp_path)
    state_report = tmp_path / "state-review.json"
    create_state_template(plan_path, state_report)
    payload = json.loads(state_report.read_text())
    payload["reviewed_at"] = "2026-11-10T12:00:00+00:00"
    for item in payload["items"]:
        item.update(
            {
                "old_kavita_id": 10,
                "new_kavita_id": 20,
                "reading_progress": "preserved-manually",
                "custom_cover": "preserved-manually",
                "resolution_note": "checked through supported UI/API",
                **updates,
            }
        )
    state_report.write_text(json.dumps(payload))

    with pytest.raises(ReconciliationRefused, match="progress/cover review"):
        approve_plan(plan_path, digest, state_report)


def test_stale_destination_or_source_provenance_cannot_be_approved(tmp_path: Path) -> None:
    config, state, archive, source, old, new = _fixture(tmp_path)
    report = state / "kavita-reports" / "publications.json"
    payload = json.loads(report.read_text())
    payload["items"][0]["destination_sha256"] = "0" * 64
    report.write_text(json.dumps(payload))
    plan_path = tmp_path / "reconciliation.json"
    _, digest = create_plan(
        [(old, new)], archive, plan_path, config, state_root=state
    )
    state_report = _approve_state_only(plan_path, tmp_path)

    with pytest.raises(ReconciliationRefused, match="manual-review"):
        approve_plan(plan_path, digest, state_report)


def test_unapproved_publication_report_cannot_authorize_supersession(
    tmp_path: Path,
) -> None:
    config, state, archive, _, old, new = _fixture(tmp_path)
    report = state / "kavita-reports" / "publications.json"
    payload = json.loads(report.read_text())
    payload["approved_apply"] = False
    report.write_text(json.dumps(payload))
    plan_path = tmp_path / "reconciliation.json"
    _, digest = create_plan(
        [(old, new)], archive, plan_path, config, state_root=state
    )
    state_report = _approve_state_only(plan_path, tmp_path)

    with pytest.raises(ReconciliationRefused, match="manual-review"):
        approve_plan(plan_path, digest, state_report)


def test_changed_incoming_source_cannot_authorize_supersession(tmp_path: Path) -> None:
    config, state, archive, source, old, new = _fixture(tmp_path)
    source.write_bytes(source.read_bytes() + b"changed")
    plan_path = tmp_path / "reconciliation.json"
    _, digest = create_plan(
        [(old, new)], archive, plan_path, config, state_root=state
    )
    state_report = _approve_state_only(plan_path, tmp_path)
    with pytest.raises(ReconciliationRefused, match="manual-review"):
        approve_plan(plan_path, digest, state_report)


def _approve_state_only(plan_path: Path, root: Path) -> Path:
    state_report = root / "review-only.json"
    create_state_template(plan_path, state_report)
    payload = json.loads(state_report.read_text())
    payload["reviewed_at"] = "2026-11-10T12:00:00+00:00"
    for item in payload["items"]:
        item.update(
            {
                "old_kavita_id": 10,
                "new_kavita_id": 20,
                "reading_progress": "none",
                "custom_cover": "none",
                "resolution_note": "reviewed",
            }
        )
    state_report.write_text(json.dumps(payload))
    return state_report


def test_old_book_one_sharing_canonical_folder_moves_only_exact_file(tmp_path: Path) -> None:
    config, state, archive, _, old, new, plan_path, plan, digest = _plan(tmp_path)
    canonical_siblings = []
    for number in range(2, 6):
        sibling = create_epub(
            old.parent / f"Dungeon Crawler Carl - 00{number} - Book {number}.epub",
            version="2.0",
            title=f"Book {number}",
            author="Matt Dinniman",
            embedded_series=("Dungeon Crawler Carl", str(number)),
        )
        canonical_siblings.append((sibling, sha256_file(sibling)))
    _approve(plan_path, plan, digest, tmp_path)

    result = execute(plan_path, config)

    archived = Path(plan["items"][0]["archive"])
    assert result["state"] == "complete"
    assert archived.is_file() and sha256_file(archived) == plan["items"][0]["old"]["sha256"]
    assert not old.exists()
    assert new.is_file()
    assert all(
        path.is_file() and sha256_file(path) == digest
        for path, digest in canonical_siblings
    )
    report = state / "supersession-reports" / f"{plan['plan_id']}.json"
    assert json.loads(report.read_text())["report_schema"] == "kavita-ingest-supersession-v1"
    assert archive.is_dir()


def test_five_pair_plan_preserves_every_canonical_publication(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    canonical = books / "Dungeon Crawler Carl"
    state = tmp_path / "state"
    archive = tmp_path / "archive"
    for path in (incoming, canonical, state / "kavita-reports", archive):
        path.mkdir(parents=True, exist_ok=True)
    database = state / "state.sqlite3"
    migrate(database)
    config = AppConfig(
        incoming_roots=(incoming,),
        books_root=books,
        comics_root=tmp_path / "comics",
        database_path=database,
    )
    pairs: list[tuple[Path, Path]] = []
    report_items: list[dict[str, object]] = []
    for number in range(1, 6):
        title = f"Dungeon Crawler Carl Book {number}"
        source = create_epub(
            incoming / f"source-{number}.epub",
            version="2.0",
            title=title,
            author="Matt Dinniman",
            embedded_series=None,
        )
        old_folder = canonical if number == 1 else books / f"Legacy Book {number}"
        old_folder.mkdir(parents=True, exist_ok=True)
        old = create_epub(
            old_folder / f"legacy-{number}.epub",
            version="2.0",
            title=title,
            author="Matt Dinniman",
            embedded_series=None,
        )
        new = create_epub(
            canonical / f"Dungeon Crawler Carl - {number:03d} - Book {number}.epub",
            version="2.0",
            title=title,
            author="Matt Dinniman",
            embedded_series=("Dungeon Crawler Carl", str(number)),
        )
        pairs.append((old, new))
        report_items.extend(
            {
                "item_state": "complete",
                "cleanup_eligible": True,
                "source_lifecycle": "preserved",
                "action": "published",
                "source": str(source),
                "source_sha256": sha256_file(source),
                "destination": str(destination),
                "destination_sha256": sha256_file(destination),
            }
            for destination in (old, new)
        )
    (state / "kavita-reports" / "publications.json").write_text(
        json.dumps(
            {
                "report_schema": "kavita-ingest-publication-v3",
                "status": "completed",
                "recovery_required": False,
                "approved_apply": True,
                "source_lifecycle": "preserved",
                "verification_completed": True,
                "items": report_items,
            }
        )
    )
    plan_path = tmp_path / "reconciliation.json"
    plan, digest = create_plan(
        pairs, archive, plan_path, config, state_root=state
    )
    state_report = _approve_state_only(plan_path, tmp_path)
    approve_plan(plan_path, digest, state_report)
    canonical_hashes = {new: sha256_file(new) for _, new in pairs}

    result = execute(plan_path, config)

    assert result["state"] == "complete"
    assert len(plan["items"]) == 5
    assert all(not old.exists() for old, _ in pairs)
    assert all(
        new.is_file() and sha256_file(new) == digest
        for new, digest in canonical_hashes.items()
    )
    assert all(Path(item["archive"]).is_file() for item in plan["items"])


def test_preview_is_read_only_and_missing_canonical_fails_execution(tmp_path: Path) -> None:
    config, _, _, _, old, new, plan_path, plan, digest = _plan(tmp_path)
    _approve(plan_path, plan, digest, tmp_path)
    old_hash = sha256_file(old)
    new_hash = sha256_file(new)

    result = preview(plan_path)

    assert result["items"][0]["old_exists"] is True
    assert sha256_file(old) == old_hash
    assert sha256_file(new) == new_hash
    assert not Path(plan["items"][0]["archive"]).exists()

    new.unlink()
    with pytest.raises((OSError, ReconciliationRefused)):
        execute(plan_path, config)


def test_interrupted_move_is_recovered_idempotently_and_rolls_back(tmp_path: Path) -> None:
    config, _, _, _, old, new, plan_path, plan, digest = _plan(tmp_path)
    _approve(plan_path, plan, digest, tmp_path)
    archived = Path(plan["items"][0]["archive"])
    archived.parent.mkdir(parents=True)
    old.rename(archived)

    recovered = execute(plan_path, config)
    repeated = execute(plan_path, config)
    rolled_back = execute(plan_path, config, rollback=True)

    assert recovered["state"] == repeated["state"] == "complete"
    assert rolled_back["state"] == "rolled_back"
    assert old.is_file() and not archived.exists() and new.is_file()


def test_changed_old_file_and_archive_collision_fail_closed(tmp_path: Path) -> None:
    config, _, _, _, old, _, plan_path, plan, digest = _plan(tmp_path)
    _approve(plan_path, plan, digest, tmp_path)
    old.write_bytes(old.read_bytes() + b"changed")
    with pytest.raises(ReconciliationRefused, match="changed after plan"):
        execute(plan_path, config)

    old.unlink()
    old.write_bytes(b"replacement")
    archived = Path(plan["items"][0]["archive"])
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"collision")
    with pytest.raises(ReconciliationRefused, match="unsafe old/archive state"):
        execute(plan_path, config)
