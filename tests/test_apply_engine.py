from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from compatibility.helpers.epub_factory import opf_snapshot
from kavita_ingest.apply_engine import (
    ApplyEngine,
    ApplyRefused,
    InjectedCrash,
    PlanAlreadyComplete,
    RecoveryRequired,
    StalePlan,
    WriterDispatcher,
)
from kavita_ingest.apply_journal import ItemState, JournalRepository, RunState
from kavita_ingest.comicinfo import ComicInfoError
from kavita_ingest.db import connect
from kavita_ingest.filesystem import LinuxFilesystem, sha256_file
from kavita_ingest.locking import LockUnavailable, ProcessLock, lock_path
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.rollback import preview_rollback
from kavita_ingest.writers.common import VerificationResult
from tests.apply_helpers import make_apply_fixture


@pytest.mark.parametrize("media_format", ["epub", "cbz", "cbr", "pdf"])
def test_successful_apply_stages_verifies_commits_then_removes_source(
    media_format: str, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, media_format)
    source_hash = sha256_file(fixture.source)
    summary = ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert summary.status is RunState.COMPLETE
    assert summary.counts == {"complete": 1}
    assert not fixture.source.exists()
    assert fixture.destination.is_file()
    if media_format == "cbr":
        assert fixture.destination.suffix == ".cbz"
        assert sha256_file(fixture.destination) != source_hash





def test_schema2_plan_remains_auditable_but_cannot_apply_under_signature_semantics(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name="legacy-signature-plan",
        approve=False,
    )
    database = fixture.config.database_path
    assert database is not None

    with connect(database) as connection:
        store = PlanStore(connection)
        current = store.get(fixture.plan_id)
        document = json.loads(current.canonical_json)
        document["schema_version"] = 2
        del document["items"][0]["source"]["signature"]
        payload = json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        legacy = store.import_bytes(payload)
        store.approve(legacy.id, legacy.sha256)

    with pytest.raises(ApplyRefused, match="predates immutable content-signature"):
        ApplyEngine(fixture.config).apply(legacy.id)

    assert fixture.source.exists()
    assert not fixture.destination.exists()

def test_zip_container_with_cbr_suffix_applies_as_cbz_without_suffix_trust(tmp_path: Path) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name="battle-beast-disguised-cbr",
        disguised_cbz_suffix=".cbr",
    )

    summary = ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert summary.status is RunState.COMPLETE
    assert not fixture.source.exists()
    assert fixture.destination.suffix == ".cbz"
    assert fixture.destination.is_file()


def test_pdf_comic_uses_pdf_safe_projection_and_completes_apply(tmp_path: Path) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "pdf",
        plan_name="doomsday-clock-pdf-comic",
        comic_pdf=True,
    )

    summary = ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert summary.status is RunState.COMPLETE
    assert not fixture.source.exists()
    assert fixture.destination.is_file()
    assert fixture.destination.parent.name == "Doomsday Clock (2017)"
    assert fixture.destination.name.startswith("Doomsday Clock (2017) - 001")

def test_work_only_epub_preserves_unresolved_edition_metadata(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "epub", work_only=True)
    before = opf_snapshot(fixture.source)
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    after = opf_snapshot(fixture.destination)
    assert after["title"] == "Resolved Book"
    assert after["publisher"] == before["publisher"]
    assert after["date"] == before["date"]
    assert after["language"] == before["language"]
    assert after["identifiers"] == before["identifiers"]


def test_preserve_and_archive_lifecycles(tmp_path: Path) -> None:
    preserved = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", plan_name="preserve")
    archived = make_apply_fixture(
        tmp_path, "pdf", lifecycle="archive_after_verify", plan_name="archive"
    )
    preserved_hash = sha256_file(preserved.source)
    archived_hash = sha256_file(archived.source)
    ApplyEngine(preserved.config).apply(preserved.plan_id)
    ApplyEngine(archived.config).apply(archived.plan_id)
    assert preserved.source.is_file() and sha256_file(preserved.source) == preserved_hash
    assert archived.archive and archived.archive.is_file()
    assert sha256_file(archived.archive) == archived_hash
    assert not archived.source.exists()


def test_unapproved_plan_and_completed_plan_cannot_start(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", approve=False)
    with pytest.raises(ApplyRefused, match="not explicitly approved"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)

    complete = make_apply_fixture(tmp_path, "cbz", plan_name="complete")
    ApplyEngine(complete.config).apply(complete.plan_id)
    with pytest.raises(PlanAlreadyComplete):
        ApplyEngine(complete.config).apply(complete.plan_id)
    assert ApplyEngine(complete.config).recover(complete.plan_id).status is RunState.COMPLETE

    with pytest.raises(ApplyRefused, match="does not exist"):
        ApplyEngine(complete.config).apply(999_999)


def test_stale_or_missing_source_and_preexisting_destination_refuse_before_writes(
    tmp_path: Path,
) -> None:
    changed = make_apply_fixture(tmp_path, "cbz", plan_name="changed")
    changed.source.write_bytes(b"changed")
    with pytest.raises(StalePlan, match="size changed|fingerprint changed"):
        ApplyEngine(changed.config).apply(changed.plan_id)
    assert not changed.destination.exists()

    missing = make_apply_fixture(tmp_path, "cbz", plan_name="missing")
    missing.source.unlink()
    with pytest.raises(StalePlan, match="missing"):
        ApplyEngine(missing.config).apply(missing.plan_id)

    collision = make_apply_fixture(tmp_path, "cbz", plan_name="collision")
    collision.destination.parent.mkdir(parents=True)
    collision.destination.write_bytes(b"existing")
    with pytest.raises(StalePlan, match="destination now exists"):
        ApplyEngine(collision.config).apply(collision.plan_id)
    assert collision.source.exists() and collision.destination.read_bytes() == b"existing"


def test_disk_space_refusal_is_plan_wide_and_source_safe(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    engine = ApplyEngine(
        fixture.config,
        disk_usage=lambda path: SimpleNamespace(total=100, used=99, free=1),
    )
    with pytest.raises(StalePlan, match="insufficient destination space"):
        engine.apply(fixture.plan_id)
    assert fixture.source.exists() and not fixture.destination.exists()


def test_disk_space_preflight_aggregates_items_on_one_filesystem(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    engine = ApplyEngine(
        fixture.config,
        disk_usage=lambda path: SimpleNamespace(
            total=128 * 1024 * 1024,
            used=28 * 1024 * 1024,
            free=100 * 1024 * 1024,
        ),
    )
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        _, document = engine._eligible_plan(connection, fixture.plan_id)
    first = engine._prepare_items(document, run_id="preflight")[0]
    second = replace(
        first,
        item_id="item-2",
        destination=first.destination.with_name(f"second-{first.destination.name}"),
    )
    errors = engine._preflight_all((first, second))
    assert set(errors) == {"item-1", "item-2"}
    assert all("shared filesystem" in messages[0] for messages in errors.values())


class FailingWriter(WriterDispatcher):
    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        destination.write_bytes(b"partial-diagnostic-stage")
        raise OSError("synthetic writer failure")


class FailingVerifier(WriterDispatcher):
    def verify(self, item, candidate):  # type: ignore[no-untyped-def]
        return VerificationResult(False, ("synthetic",), ("synthetic verification failure",))


class SelectiveMetadataFailureWriter(WriterDispatcher):
    def __init__(self, failures: set[str]) -> None:
        self.failures = failures
        self.staged: list[str] = []

    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        self.staged.append(item.item_id)
        if item.item_id in self.failures:
            raise ComicInfoError("synthetic deterministic ComicInfo incompatibility")
        return super().stage(item, destination)


class RecordingWriter(WriterDispatcher):
    def __init__(self) -> None:
        self.staged: list[str] = []

    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        self.staged.append(item.item_id)
        return super().stage(item, destination)


@pytest.mark.parametrize("writers", [FailingWriter(), FailingVerifier()])
def test_writer_or_verifier_failure_keeps_source_and_never_publishes(
    writers: WriterDispatcher, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    summary = ApplyEngine(fixture.config, writers=writers).apply(fixture.plan_id)
    assert summary.status is RunState.RECOVERY_REQUIRED
    assert fixture.source.exists() and not fixture.destination.exists()


def test_recovery_retries_only_four_failed_items_from_18_complete(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", plan_name="multi-recovery")
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        original = PlanStore(connection).get(fixture.plan_id)
        document = json.loads(original.canonical_json)
        template = document["items"][0]
        items = []
        for number in range(1, 23):
            item = copy.deepcopy(template)
            source = fixture.source.with_name(f"issue-{number:03}.cbz")
            if source != fixture.source:
                shutil.copy2(fixture.source, source)
            destination = fixture.destination.with_name(f"issue-{number:03}.cbz")
            item["item_id"] = f"issue-{number:03}"
            item["source"]["path"] = str(source)
            item["source"]["size"] = source.stat().st_size
            item["source"]["mtime_ns"] = source.stat().st_mtime_ns
            item["kavita_projection"]["destination"] = str(
                destination.relative_to(fixture.config.comics_root)  # type: ignore[arg-type]
            )
            item["kavita_projection"]["absolute_destination"] = str(destination)
            items.append(item)
        document["plan_id"] = "multi-recovery-18-complete-4-failed"
        document["items"] = items
        payload = json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        plan = PlanStore(connection).import_bytes(payload)
        PlanStore(connection).approve(plan.id, plan.sha256)

    failed_ids = {"issue-005", "issue-017", "issue-018", "issue-020"}
    initial_writer = SelectiveMetadataFailureWriter(failed_ids)
    first = ApplyEngine(fixture.config, writers=initial_writer).apply(plan.id)
    assert first.counts == {"complete": 18, "failed": 4}
    completed_evidence = {
        item_id: (
            fixture.destination.with_name(f"{item_id}.cbz").stat().st_mtime_ns,
            sha256_file(fixture.destination.with_name(f"{item_id}.cbz")),
        )
        for item_id in set(initial_writer.staged) - failed_ids
    }
    assert all(
        fixture.source.with_name(f"{item_id}.cbz").exists() for item_id in failed_ids
    )

    recovery_writer = RecordingWriter()
    recovered = ApplyEngine(fixture.config, writers=recovery_writer).recover(plan.id)

    assert recovered.status is RunState.COMPLETE
    assert set(recovery_writer.staged) == failed_ids
    for item_id, evidence in completed_evidence.items():
        destination = fixture.destination.with_name(f"{item_id}.cbz")
        assert (destination.stat().st_mtime_ns, sha256_file(destination)) == evidence


class RacingFilesystem(LinuxFilesystem):
    def commit(self, staged: Path, destination: Path) -> None:
        destination.write_bytes(b"racing-file")
        super().commit(staged, destination)


def test_atomic_commit_race_never_overwrites_new_destination(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    summary = ApplyEngine(fixture.config, filesystem=RacingFilesystem()).apply(fixture.plan_id)
    assert summary.status is RunState.FAILED
    assert fixture.source.exists()
    assert fixture.destination.read_bytes() == b"racing-file"


class CrashAfterHardLinkFilesystem(LinuxFilesystem):
    def commit(self, staged: Path, destination: Path) -> None:
        os.link(staged, destination, follow_symlinks=False)
        self.make_file_durable(destination)
        raise InjectedCrash("after-link-before-stage-unlink")


class VerifyOnlyRecoveryWriter(WriterDispatcher):
    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        raise AssertionError("a VERIFIED stage must never be passed to a writer again")


def test_verified_hard_link_stage_is_never_modified_and_recovery_only_unlinks_name(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, filesystem=CrashAfterHardLinkFilesystem()).apply(
            fixture.plan_id
        )
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        journal = JournalRepository(connection)
        run = journal.latest_for_plan(fixture.plan_id)
        assert run is not None
        recorded = journal.items(run.id)[0]
    assert recorded.staging_path is not None
    staging = Path(recorded.staging_path)
    assert staging.stat().st_ino == fixture.destination.stat().st_ino
    published = fixture.destination.read_bytes()

    summary = ApplyEngine(fixture.config, writers=VerifyOnlyRecoveryWriter()).recover(
        fixture.plan_id
    )

    assert summary.status is RunState.COMPLETE
    assert fixture.destination.read_bytes() == published
    assert not staging.exists()


def test_process_lock_contention_blocks_apply(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    database = fixture.config.database_path
    assert database is not None
    with ProcessLock(lock_path(database)), pytest.raises(LockUnavailable):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.source.exists() and not fixture.destination.exists()


class CleanupFailFilesystem(LinuxFilesystem):
    def durable_unlink(self, path: Path) -> None:
        raise OSError("synthetic cleanup failure")


def test_cleanup_failure_retains_source_and_recovery_finishes(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    summary = ApplyEngine(fixture.config, filesystem=CleanupFailFilesystem()).apply(fixture.plan_id)
    assert summary.status is RunState.RECOVERY_REQUIRED
    assert fixture.destination.exists() and fixture.source.exists()
    recovered = ApplyEngine(fixture.config).recover(fixture.plan_id)
    assert recovered.status is RunState.COMPLETE
    assert fixture.destination.exists() and not fixture.source.exists()


@pytest.mark.parametrize(
    "checkpoint",
    [
        "before_staging",
        "during_writer",
        "after_staged_write",
        "after_staged",
        "after_staging_verification",
        "after_verified_journal",
        "during_destination_commit",
        "after_destination_commit",
        "after_committed",
        "before_cleanup",
        "during_cleanup",
        "after_cleanup_filesystem",
    ],
)
def test_recovery_matrix_is_idempotent(checkpoint: str, tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", plan_name=checkpoint)

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == checkpoint:
            raise InjectedCrash(checkpoint)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    with pytest.raises(RecoveryRequired):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    first = ApplyEngine(fixture.config).recover(fixture.plan_id)
    second = ApplyEngine(fixture.config).recover(fixture.plan_id)
    assert first.status is RunState.COMPLETE
    assert second.status is RunState.COMPLETE
    assert fixture.destination.exists() and not fixture.source.exists()


def test_recovery_recognizes_matching_crash_commit_and_rejects_changed_destination(
    tmp_path: Path,
) -> None:
    matching = make_apply_fixture(tmp_path, "cbz", plan_name="matching")

    def crash_after_commit(name: str, item_id: str) -> None:
        del item_id
        if name == "after_destination_commit":
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(matching.config, fault=crash_after_commit).apply(matching.plan_id)
    assert matching.destination.exists() and matching.source.exists()
    assert ApplyEngine(matching.config).recover(matching.plan_id).status is RunState.COMPLETE

    changed = make_apply_fixture(tmp_path, "cbz", plan_name="wrong-destination")
    with pytest.raises(InjectedCrash):
        ApplyEngine(changed.config, fault=crash_after_commit).apply(changed.plan_id)
    changed.destination.write_bytes(b"changed-after-crash")
    summary = ApplyEngine(changed.config).recover(changed.plan_id)
    assert summary.status is RunState.RECOVERY_REQUIRED
    assert changed.source.exists()


def test_source_change_after_preflight_is_journalled_stale(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    def mutate(name: str, item_id: str) -> None:
        del item_id
        if name == "before_staging":
            fixture.source.write_bytes(b"changed-after-preflight")

    summary = ApplyEngine(fixture.config, fault=mutate).apply(fixture.plan_id)
    assert summary.status is RunState.FAILED
    assert summary.counts == {"stale": 1}
    assert not fixture.destination.exists()


def test_archive_collision_never_deletes_source(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "pdf", lifecycle="archive_after_verify")
    assert fixture.archive is not None
    fixture.archive.write_bytes(b"existing-archive")
    with pytest.raises(StalePlan, match="archive destination now exists"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.source.exists() and fixture.archive.read_bytes() == b"existing-archive"


@pytest.mark.parametrize("pdf_state", ["encrypted", "signed"])
def test_apply_preflight_blocks_unsafe_pdf_states(pdf_state: str, tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "pdf", pdf_state=pdf_state)
    with pytest.raises(StalePlan, match="encrypted|signature|password"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.source.exists() and not fixture.destination.exists()


def test_apply_preflight_blocks_cbr_links(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbr", cbr_fixture="rar5-symlink-unix.rar")
    with pytest.raises(StalePlan, match="links are unsupported"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.source.exists() and not fixture.destination.exists()


def test_source_disappearing_during_cleanup_is_recovered_only_after_commit(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    def disappear(name: str, item_id: str) -> None:
        del item_id
        if name == "during_cleanup":
            fixture.source.unlink()

    summary = ApplyEngine(fixture.config, fault=disappear).apply(fixture.plan_id)
    assert summary.status is RunState.RECOVERY_REQUIRED
    assert fixture.destination.exists() and not fixture.source.exists()
    assert ApplyEngine(fixture.config).recover(fixture.plan_id).status is RunState.COMPLETE


def test_archive_collision_during_cleanup_retains_original_and_destination(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "pdf", lifecycle="archive_after_verify")
    assert fixture.archive is not None

    def collide(name: str, item_id: str) -> None:
        del item_id
        if name == "before_cleanup":
            fixture.archive.write_bytes(b"unexpected")

    summary = ApplyEngine(fixture.config, fault=collide).apply(fixture.plan_id)
    assert summary.status is RunState.RECOVERY_REQUIRED
    assert fixture.destination.exists() and fixture.source.exists()
    assert fixture.archive.read_bytes() == b"unexpected"
    recovered = ApplyEngine(fixture.config).recover(fixture.plan_id)
    assert recovered.status is RunState.RECOVERY_REQUIRED


def test_db_failure_after_filesystem_commit_recovers_from_committing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    original = JournalRepository.transition

    def fail_committed(self, run_id, item_id, to_state, **kwargs):  # type: ignore[no-untyped-def]
        if to_state.value == "committed":
            raise sqlite3.OperationalError("synthetic COMMITTED journal failure")
        return original(self, run_id, item_id, to_state, **kwargs)

    monkeypatch.setattr(JournalRepository, "transition", fail_committed)
    with pytest.raises(sqlite3.OperationalError, match="synthetic"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.destination.exists() and fixture.source.exists()
    monkeypatch.setattr(JournalRepository, "transition", original)
    assert ApplyEngine(fixture.config).recover(fixture.plan_id).status is RunState.COMPLETE


def test_abandon_mixed_complete_and_failed_run_preserves_media_and_history(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name="abandon-mixed",
    )
    database = fixture.config.database_path
    assert database is not None

    with connect(database) as connection:
        original = PlanStore(connection).get(fixture.plan_id)
        document = json.loads(original.canonical_json)
        first = document["items"][0]
        second = copy.deepcopy(first)

        second_source = fixture.source.with_name("second.cbz")
        shutil.copy2(fixture.source, second_source)
        second_destination = fixture.destination.with_name("second.cbz")

        second["item_id"] = "item-2"
        second["source"]["path"] = str(second_source)
        second["source"]["sha256"] = sha256_file(second_source)
        second["source"]["size"] = second_source.stat().st_size
        second["source"]["mtime_ns"] = second_source.stat().st_mtime_ns
        second["kavita_projection"]["destination"] = str(
            second_destination.relative_to(
                fixture.config.comics_root  # type: ignore[arg-type]
            )
        )
        second["kavita_projection"]["absolute_destination"] = str(
            second_destination
        )

        document["plan_id"] = "abandon-mixed-two-items"
        document["items"] = [first, second]

        payload = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        plan = PlanStore(connection).import_bytes(payload)
        PlanStore(connection).approve(plan.id, plan.sha256)

    first_summary = ApplyEngine(
        fixture.config,
        writers=SelectiveMetadataFailureWriter({"item-2"}),
    ).apply(plan.id)

    assert first_summary.status is RunState.RECOVERY_REQUIRED
    assert first_summary.counts == {
        "complete": 1,
        "failed": 1,
    }

    def media_snapshot() -> dict[str, str]:
        roots = (
            fixture.source.parent,
            fixture.config.books_root,
            fixture.config.comics_root,
        )
        return {
            str(candidate): sha256_file(candidate)
            for root in roots
            if root is not None and root.exists()
            for candidate in root.rglob("*")
            if candidate.is_file()
        }

    before = media_snapshot()

    with connect(database) as connection:
        journal = JournalRepository(connection)
        run = journal.latest_for_plan(plan.id)
        assert run is not None

        event_count = connection.execute(
            "SELECT count(*) FROM apply_journal_events WHERE run_id=?",
            (run.id,),
        ).fetchone()[0]

        item_states = {
            item.item_id: item.state
            for item in journal.items(run.id)
        }

    assert item_states == {
        "item-1": ItemState.COMPLETE,
        "item-2": ItemState.FAILED,
    }

    abandoned = ApplyEngine(fixture.config).abandon(
        plan.id,
        reason="user requested a clean restart",
    )

    assert abandoned.status is RunState.FAILED

    # Abandonment is journal/database-only. Absolutely no media may change.
    assert media_snapshot() == before

    with connect(database) as connection:
        journal = JournalRepository(connection)
        closed = journal.latest_for_plan(plan.id)
        assert closed is not None

        assert closed.status is RunState.FAILED
        assert (
            closed.error
            == "abandoned by user: user requested a clean restart"
        )

        invalidation = connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?",
            (plan.id,),
        ).fetchone()

        assert invalidation is not None
        assert invalidation["reason"] == closed.error

        assert {
            item.item_id: item.state
            for item in journal.items(closed.id)
        } == item_states

        # Existing history remains and one new run-state audit event is appended.
        assert connection.execute(
            "SELECT count(*) FROM apply_journal_events WHERE run_id=?",
            (closed.id,),
        ).fetchone()[0] == event_count + 1

    inspection = ApplyEngine(
        fixture.config
    ).inspect_recovery(plan.id)

    assert all(
        "plan is invalidated" in item.proposed_action
        for item in inspection
    )


@pytest.mark.parametrize(
    ("checkpoint", "expected_state"),
    [
        ("after_staged", ItemState.STAGED),
        ("after_verified_journal", ItemState.VERIFIED),
        ("after_destination_commit", ItemState.COMMITTING),
        ("after_committed", ItemState.COMMITTED),
        ("before_cleanup", ItemState.CLEANUP_PENDING),
    ],
)
def test_abandon_refuses_uncertain_filesystem_states(
    checkpoint: str,
    expected_state: ItemState,
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name=f"abandon-refuse-{expected_state.value}",
    )

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == checkpoint:
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(
            fixture.config,
            fault=crash,
        ).apply(fixture.plan_id)

    inspection = ApplyEngine(
        fixture.config
    ).inspect_recovery(fixture.plan_id)

    assert inspection[0].state is expected_state

    with pytest.raises(
        ApplyRefused,
        match="cannot be abandoned",
    ):
        ApplyEngine(fixture.config).abandon(
            fixture.plan_id,
            reason="unsafe synthetic abandonment",
        )

    with connect(
        fixture.config.database_path  # type: ignore[arg-type]
    ) as connection:
        assert connection.execute(
            "SELECT 1 FROM plan_invalidations WHERE plan_id=?",
            (fixture.plan_id,),
        ).fetchone() is None


def test_abandon_accepts_prepublication_safe_state(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name="abandon-preflight-ok",
    )

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == "before_staging":
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(
            fixture.config,
            fault=crash,
        ).apply(fixture.plan_id)

    inspection = ApplyEngine(
        fixture.config
    ).inspect_recovery(fixture.plan_id)

    assert inspection[0].state is ItemState.PREFLIGHT_OK
    assert fixture.source.exists()
    assert not fixture.destination.exists()

    summary = ApplyEngine(fixture.config).abandon(
        fixture.plan_id,
        reason="restart before staging",
    )

    assert summary.status is RunState.FAILED
    assert fixture.source.exists()
    assert not fixture.destination.exists()


def test_abandon_refuses_already_complete_run(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        lifecycle="preserve",
    )

    assert (
        ApplyEngine(fixture.config)
        .apply(fixture.plan_id)
        .status
        is RunState.COMPLETE
    )

    with pytest.raises(
        ApplyRefused,
        match="already complete",
    ):
        ApplyEngine(fixture.config).abandon(
            fixture.plan_id,
            reason="should not be possible",
        )


def test_invalidated_plan_and_incompatible_writer_are_refused(tmp_path: Path) -> None:
    invalid = make_apply_fixture(tmp_path, "cbz", plan_name="invalid")
    with connect(invalid.config.database_path) as connection:  # type: ignore[arg-type]
        connection.execute(
            "INSERT INTO plan_invalidations(plan_id, reason, invalidated_at) VALUES (?, ?, ?)",
            (invalid.plan_id, "fixture invalidation", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    with pytest.raises(ApplyRefused, match="invalidated"):
        ApplyEngine(invalid.config).apply(invalid.plan_id)

    incompatible = make_apply_fixture(
        tmp_path,
        "cbz",
        plan_name="incompatible",
        writer_versions={"comicinfo_schema": "99.0"},
    )
    with pytest.raises(StalePlan, match="capability mismatch"):
        ApplyEngine(incompatible.config).apply(incompatible.plan_id)
    assert incompatible.source.exists() and not incompatible.destination.exists()


def test_apply_and_recovery_do_not_build_or_query_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provider runtime must not be used by apply")

    monkeypatch.setattr("kavita_ingest.provider_runtime.build_providers", forbidden)
    assert ApplyEngine(fixture.config).apply(fixture.plan_id).status is RunState.COMPLETE


def test_recovery_inspection_explains_last_state_and_safe_action(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == "after_destination_commit":
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    inspection = ApplyEngine(fixture.config).inspect_recovery(fixture.plan_id)
    assert inspection[0].state.value == "committing"
    assert inspection[0].destination_matches is True
    assert "recognize verified destination commit" in inspection[0].proposed_action


def test_rollback_preview_has_reversible_and_irreversible_cases(tmp_path: Path) -> None:
    preserved = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", plan_name="rb-yes")
    removed = make_apply_fixture(tmp_path, "cbz", plan_name="rb-no")
    ApplyEngine(preserved.config).apply(preserved.plan_id)
    ApplyEngine(removed.config).apply(removed.plan_id)
    reversible = preview_rollback(preserved.config.database_path, preserved.plan_id)  # type: ignore[arg-type]
    irreversible = preview_rollback(removed.config.database_path, removed.plan_id)  # type: ignore[arg-type]
    assert reversible[0].reversible
    assert reversible[0].action == "remove_unchanged_destination"
    assert not irreversible[0].reversible
    assert irreversible[0].action == "impossible"
    preserved.destination.write_bytes(b"changed-after-ingestion")
    changed = preview_rollback(preserved.config.database_path, preserved.plan_id)  # type: ignore[arg-type]
    assert not changed[0].reversible and changed[0].action == "refuse"


def test_journal_uses_full_synchronous_durability(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        JournalRepository(connection)
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_publication_uses_immutable_file_and_new_directory_modes(tmp_path: Path) -> None:
    fixture = make_apply_fixture(
        tmp_path, "cbz", lifecycle="preserve", file_mode=0o664, directory_mode=0o775
    )

    summary = ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert summary.status is RunState.COMPLETE
    assert fixture.destination.stat().st_mode & 0o777 == 0o664
    assert fixture.destination.parent.stat().st_mode & 0o777 == 0o775


def test_existing_directory_and_source_modes_are_not_changed(tmp_path: Path) -> None:
    fixture = make_apply_fixture(
        tmp_path, "cbz", lifecycle="preserve", file_mode=0o644, directory_mode=0o775
    )
    fixture.destination.parent.mkdir(parents=True)
    os.chmod(fixture.destination.parent, 0o700)
    os.chmod(fixture.source, 0o600)

    ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert fixture.destination.parent.stat().st_mode & 0o777 == 0o700
    assert fixture.source.stat().st_mode & 0o777 == 0o600


class ModeFailureFilesystem(LinuxFilesystem):
    def set_file_mode(self, path: Path, mode: int) -> None:
        del path, mode
        raise PermissionError("synthetic chmod failure")


def test_mode_failure_never_completes_or_removes_source_and_can_recover(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    failed = ApplyEngine(fixture.config, filesystem=ModeFailureFilesystem()).apply(
        fixture.plan_id
    )
    assert failed.status is RunState.RECOVERY_REQUIRED
    assert fixture.source.exists() and not fixture.destination.exists()

    recovered = ApplyEngine(fixture.config).recover(fixture.plan_id)
    assert recovered.status is RunState.COMPLETE
    assert fixture.destination.stat().st_mode & 0o777 == 0o644


def test_apply_progress_hook_reports_item_start_and_completion(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    events: list[tuple[int, int, str]] = []

    summary = ApplyEngine(fixture.config, progress=lambda *event: events.append(event)).apply(
        fixture.plan_id
    )

    assert summary.status is RunState.COMPLETE
    assert events == [
        (0, 1, fixture.source.name),
        (1, 1, fixture.source.name),
    ]
