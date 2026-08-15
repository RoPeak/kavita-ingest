from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.apply_engine import ApplyEngine, InjectedCrash, WriterDispatcher
from kavita_ingest.apply_journal import RunState
from kavita_ingest.cli import app
from kavita_ingest.completed_sources import assess_completed_sources
from kavita_ingest.config import AppConfig, load_config
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import (
    DecisionRepository,
    DecisionType,
    add_manual_identity,
    add_manual_override,
)
from kavita_ingest.domain import SourceRecord
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.planning_service import PlanBuilder
from kavita_ingest.review import _mark_incompatible_group_decisions
from kavita_ingest.run_groups import run_group_key
from kavita_ingest.scanner import scan
from kavita_ingest.wizard import (
    DiscoverySelection,
    _incomplete_review_items,
    _render_build_result,
    _select_discovered_sources,
    _select_root,
    detect_resume_state,
    latest_invalidation_notice,
)
from tests.apply_helpers import ApplyFixture, make_apply_fixture


def _config(path: Path, fixture: ApplyFixture) -> Path:
    config = path / "wizard.toml"
    config.write_text(
        f'''[paths]
database = "{fixture.config.database_path}"
incoming = ["{fixture.source.parent}"]
books = "{fixture.config.books_root}"
comics = "{fixture.config.comics_root}"

[source]
lifecycle = "preserve"

[providers]
offline = true

[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )
    return config


def test_wizard_resumes_draft_and_binds_full_digest_without_applying(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    config = _config(tmp_path, fixture)

    result = CliRunner().invoke(app, ["wizard", "--config", str(config)], input="r\na\nn\n")

    assert result.exit_code == 0, result.output
    assert "A draft plan is ready to review" in result.output
    assert "exact displayed plan digest is now locked" in result.output
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    assert plan.status == "approved" and plan.approval_digest == plan.sha256
    assert fixture.source.exists() and not fixture.destination.exists()


def test_wizard_resumes_approved_plan_without_reapproval(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "Approve this exact" not in result.output
    assert fixture.source.exists() and not fixture.destination.exists()


@pytest.mark.parametrize(("media_format", "work_only"), [("cbz", False), ("epub", True)])
def test_wizard_applies_approved_comic_and_work_only_epub_with_production_engine(
    media_format: str, work_only: bool, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(
        tmp_path, media_format, lifecycle="preserve", work_only=work_only
    )
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\ny\nq\n"
    )
    assert result.exit_code == 0, result.output
    assert "Ingest complete" in result.output
    assert fixture.source.exists() and fixture.destination.exists()


def test_wizard_makes_recovery_prominent_and_uses_existing_engine(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")

    def crash(checkpoint: str, item_id: str) -> None:
        del item_id
        if checkpoint == "after_verified_journal":
            raise InjectedCrash(checkpoint)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\ny\nq\n"
    )
    assert result.exit_code == 0, result.output
    assert "interrupted ingest" in result.output and "Ingest complete" in result.output
    assert fixture.destination.exists()


class _WizardFailingWriter(WriterDispatcher):
    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        del item, destination
        raise OSError("synthetic wizard abandonment failure")


def test_wizard_can_abandon_recoverable_run_without_touching_media(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    first = ApplyEngine(
        fixture.config,
        writers=_WizardFailingWriter(),
    ).apply(fixture.plan_id)

    assert first.status is RunState.RECOVERY_REQUIRED

    result = CliRunner().invoke(
        app,
        [
            "wizard",
            "--config",
            str(_config(tmp_path, fixture)),
        ],
        input=(
            "a\n"
            "y\n"
            "wizard regression restart\n"
            "q\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Abandon this ingest" in result.output
    assert "closed as abandoned" in result.output
    assert "No media files were modified" in result.output

    assert fixture.source.exists()
    assert not fixture.destination.exists()

    assert detect_resume_state(fixture.config) is None

    status = CliRunner().invoke(
        app, ["status", "--config", str(_config(tmp_path, fixture))]
    )
    assert status.exit_code == 0, status.output
    assert "Recovery needed" not in status.output
    assert "Last ingest closed (failed)" in status.output
    assert "No recovery required" in status.output


def test_resume_detection_ignores_invalidated_plan(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO plan_invalidations(plan_id, reason, invalidated_at) VALUES (?, ?, ?)",
            (fixture.plan_id, "decision changed", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    assert detect_resume_state(fixture.config) is None


def test_wizard_explains_provider_unavailable_and_saves_unresolved_decision(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    with zipfile.ZipFile(incoming / "Unknown Series 001.cbz", "w") as archive:
        archive.writestr("001.jpg", b"page")
    config = tmp_path / "unavailable.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / 'state.sqlite3'}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[source]
lifecycle = "preserve"

[providers]
offline = true

[providers.open_library]
enabled = false
[providers.google_books]
enabled = false
[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["wizard", "--config", str(config)], input="\nu\nq\n"
    )

    assert result.exit_code == 0, result.output
    assert "Provider problems" in result.output
    assert "No plan was created" in result.output
    assert "Review remains saved" in result.output


def test_reviewed_decision_without_plan_resumes_directly_to_offline_planning(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    with zipfile.ZipFile(incoming / "Watchmen 001.cbz", "w") as archive:
        archive.writestr("001.jpg", b"page")
    config = tmp_path / "reviewed.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / 'state.sqlite3'}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[source]
lifecycle = "preserve"

[providers]
offline = true
[providers.open_library]
enabled = false
[providers.google_books]
enabled = false
[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )
    reviewed = CliRunner().invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input="i\nWatchmen\nAt Midnight\nissue\n1\n1986\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    state = detect_resume_state(load_config(config))
    assert state is not None and state.kind == "reviewed" and state.item_count == 1

    resumed = CliRunner().invoke(
        app, ["wizard", "--config", str(config)], input="r\nq\n"
    )
    assert resumed.exit_code == 0, resumed.output
    assert "providers will not be queried again" in resumed.output
    assert "Draft plan saved" in resumed.output
    with connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM plans").fetchone()[0] == 1


def _decision_resume_fixture(
    tmp_path: Path,
) -> tuple[AppConfig, Path, SourceRecord]:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"

    for directory in (incoming, books, comics):
        directory.mkdir()

    create_pdf(incoming / "Reviewed Book.pdf")

    database = tmp_path / "state.sqlite3"
    settings = AppConfig(
        incoming_roots=(incoming,),
        books_root=books,
        comics_root=comics,
        database_path=database,
        source_lifecycle="preserve",
    )
    migrate(database)

    reviewed = scan(incoming, settings, persist=True)[0]

    with connect(database) as connection:
        add_manual_identity(
            DecisionRepository(connection),
            reviewed.source,
            "initial-evidence",
            {
                "title": "Reviewed Book",
                "authors": "Alex Author",
            },
        )

    return settings, incoming, reviewed.source


def _consume_current_decision(
    settings: AppConfig,
    root: Path,
    *,
    status: str,
) -> int:
    if status not in {"complete", "failed"}:
        raise ValueError(f"unsupported synthetic historical status: {status}")

    database = settings.database_path
    assert database is not None

    with connect(database) as connection:
        result = PlanBuilder(connection, settings).build(root)

        store = PlanStore(connection)
        plan = store.add(result.document)
        plan = store.approve(plan.id, plan.sha256)

        now = "2026-08-13T09:00:00+00:00"

        connection.execute(
            "INSERT INTO apply_runs("
            "id, plan_id, plan_digest, status, started_at, "
            "updated_at, completed_at, error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"synthetic-{plan.id}-{status}",
                plan.id,
                plan.sha256,
                status,
                now,
                now,
                now,
                (
                    "abandoned by user: synthetic history"
                    if status == "failed"
                    else None
                ),
            ),
        )

        if status == "failed":
            connection.execute(
                "INSERT INTO plan_invalidations("
                "plan_id, reason, invalidated_at"
                ") VALUES (?, ?, ?)",
                (
                    plan.id,
                    "abandoned by user: synthetic history",
                    now,
                ),
            )

        connection.commit()
        return plan.id


@pytest.mark.parametrize(
    "historical_status",
    ["complete", "failed"],
)
def test_resume_is_decision_head_aware_after_historical_apply(
    historical_status: str,
    tmp_path: Path,
) -> None:
    settings, incoming, source = _decision_resume_fixture(tmp_path)
    database = settings.database_path
    assert database is not None

    plan_id = _consume_current_decision(
        settings,
        incoming,
        status=historical_status,
    )

    # The exact decision head already consumed by an apply run must not
    # silently requeue itself.
    assert detect_resume_state(settings) is None

    with connect(database) as connection:
        fresh = add_manual_identity(
            DecisionRepository(connection),
            source,
            "fresh-evidence",
            {
                "title": "Reviewed Book",
                "authors": "Alex Author",
                "publisher": "Fresh Press",
            },
        )

        consumed_heads = {
            int(row[0])
            for row in connection.execute(
                "SELECT decision_head_id "
                "FROM plan_preconditions "
                "WHERE plan_id=?",
                (plan_id,),
            ).fetchall()
        }

        assert fresh.id not in consumed_heads

    state = detect_resume_state(settings)

    assert state is not None
    assert state.kind == "reviewed"
    assert state.root == incoming.resolve()
    assert state.item_count == 1


def test_manual_override_after_consumed_identity_resumes_as_new_decision_head(
    tmp_path: Path,
) -> None:
    settings, incoming, source = _decision_resume_fixture(tmp_path)
    database = settings.database_path
    assert database is not None

    _consume_current_decision(
        settings,
        incoming,
        status="complete",
    )

    assert detect_resume_state(settings) is None

    with connect(database) as connection:
        override = add_manual_override(
            DecisionRepository(connection),
            source,
            "override-evidence",
            "publisher",
            "Corrected Publisher",
        )

        assert override.decision_type is DecisionType.MANUAL_OVERRIDE

    state = detect_resume_state(settings)

    assert state is not None
    assert state.kind == "reviewed"
    assert state.root == incoming.resolve()
    assert state.item_count == 1


def test_identical_bytes_renamed_after_consumed_plan_resume_current_path_only(
    tmp_path: Path,
) -> None:
    settings, incoming, source = _decision_resume_fixture(tmp_path)
    database = settings.database_path
    assert database is not None

    _consume_current_decision(
        settings,
        incoming,
        status="failed",
    )

    assert detect_resume_state(settings) is None

    old_path = source.path
    new_path = old_path.with_name("Renamed Reviewed Book.pdf")

    old_path.rename(new_path)

    rescanned = scan(
        incoming,
        settings,
        persist=True,
    )[0]

    assert rescanned.source.sha256 == source.sha256
    assert rescanned.source.path == new_path.resolve()

    with connect(database) as connection:
        add_manual_identity(
            DecisionRepository(connection),
            rescanned.source,
            "renamed-evidence",
            {
                "title": "Reviewed Book",
                "authors": "Alex Author",
            },
        )

    state = detect_resume_state(settings)

    assert state is not None
    assert state.kind == "reviewed"
    assert state.root == incoming.resolve()

    # The missing historical pathname must not inflate the count.
    assert state.item_count == 1


def test_unresolved_new_head_after_consumed_acceptance_resumes_review(
    tmp_path: Path,
) -> None:
    settings, incoming, source = _decision_resume_fixture(tmp_path)
    database = settings.database_path
    assert database is not None

    _consume_current_decision(
        settings,
        incoming,
        status="complete",
    )

    with connect(database) as connection:
        DecisionRepository(connection).add(
            source,
            DecisionType.UNRESOLVED,
            "unresolved-evidence",
        )

    state = detect_resume_state(settings)

    assert state is not None
    assert state.kind == "review"
    assert state.root == incoming.resolve()
    assert state.item_count == 1


def test_build_result_collapses_historical_missing_exclusions() -> None:
    buffer = io.StringIO()
    output = Console(
        file=buffer,
        force_terminal=False,
        width=160,
    )

    result = SimpleNamespace(
        accepted_included=2,
        unapproved_excluded=0,
        unresolved_blocked=1,
        exclusions=(
            SimpleNamespace(
                path="/old/Absolute Batman 001.cbz",
                category="historical_missing",
                explanation="historical reviewed source no longer exists",
            ),
            SimpleNamespace(
                path="/old/Absolute Green Lantern 001.cbz",
                category="historical_missing",
                explanation="historical reviewed source no longer exists",
            ),
            SimpleNamespace(
                path="/current/Broken Book.epub",
                category="unreadable_source",
                explanation="reviewed source could not be inspected",
            ),
        ),
    )

    _render_build_result(result, output)
    rendered = buffer.getvalue()

    assert (
        "Prepared 2 items; left out 0 unapproved, "
        "0 unresolved, 1 blocked and 0 skipped."
        in rendered
    )
    assert (
        "Ignored 2 historical source records no longer present."
        in rendered
    )

    # Historical audit rows stay in the result object but do not flood UX.
    assert "Absolute Batman 001.cbz" not in rendered
    assert "Absolute Green Lantern 001.cbz" not in rendered

    # A genuinely current problem remains visible.
    assert "Broken Book.epub" in rendered
    assert "could not be inspected" in rendered


def test_invalidation_notice_remains_until_completed_replacement(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        lifecycle="preserve",
        approve=False,
    )
    database = fixture.config.database_path
    assert database is not None

    with connect(database) as connection:
        original = PlanStore(connection).get(fixture.plan_id)

        connection.execute(
            "INSERT INTO plan_invalidations("
            "plan_id, reason, invalidated_at"
            ") VALUES (?, ?, ?)",
            (
                original.id,
                "abandoned by user: replace this ingest",
                "2026-08-13T08:00:00+00:00",
            ),
        )
        connection.commit()

    notice = latest_invalidation_notice(fixture.config)

    assert notice is not None
    assert "previous ingest was abandoned" in notice
    assert "replacement plan is still required" in notice

    # Creating/approving a replacement is not enough. It must actually
    # complete successfully before the historical warning is retired.
    with connect(database) as connection:
        original = PlanStore(connection).get(fixture.plan_id)
        document = json.loads(original.canonical_json)

        document["plan_id"] = "completed-replacement"
        document["created_at"] = "2026-08-13T09:00:00+00:00"

        payload = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        store = PlanStore(connection)
        replacement = store.import_bytes(payload)
        replacement = store.approve(
            replacement.id,
            replacement.sha256,
        )

    assert latest_invalidation_notice(fixture.config) is not None

    with connect(database) as connection:
        connection.execute(
            "INSERT INTO apply_runs("
            "id, plan_id, plan_digest, status, "
            "started_at, updated_at, completed_at, error"
            ") VALUES (?, ?, ?, 'complete', ?, ?, ?, NULL)",
            (
                "completed-replacement-run",
                replacement.id,
                replacement.sha256,
                "2026-08-13T09:05:00+00:00",
                "2026-08-13T09:06:00+00:00",
                "2026-08-13T09:06:00+00:00",
            ),
        )
        connection.commit()

    assert latest_invalidation_notice(fixture.config) is None


def test_invalidation_notice_not_needed_when_original_items_completed(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        lifecycle="preserve",
    )
    database = fixture.config.database_path
    assert database is not None

    summary = ApplyEngine(fixture.config).apply(
        fixture.plan_id
    )

    assert summary.status is RunState.COMPLETE

    # The plan can become historically invalid later, for example because
    # metadata is reviewed again. The completed publication itself does
    # not need a replacement warning.
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO plan_invalidations("
            "plan_id, reason, invalidated_at"
            ") VALUES (?, ?, ?)",
            (
                fixture.plan_id,
                "explicit identity decision changed after plan creation",
                "2026-08-13T10:00:00+00:00",
            ),
        )
        connection.commit()

    assert latest_invalidation_notice(fixture.config) is None


def test_fresh_wizard_has_one_start_action_and_full_human_review_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    config = _config(tmp_path, fixture)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        stored = PlanStore(connection).get(fixture.plan_id)
    audit = SimpleNamespace(
        items=(
            SimpleNamespace(
                local=SimpleNamespace(
                    kind=SimpleNamespace(value="comic"),
                    series_title="Watchmen",
                )
            ),
        ),
        summary={
            "sources": 1,
            "eligible_high_confidence": 1,
            "review_required": 0,
            "unresolved": 0,
            "provider_unavailable": 0,
            "partial_provider_unavailable": 0,
        },
    )
    build = SimpleNamespace(
        accepted_included=1,
        unapproved_excluded=0,
        unresolved_blocked=0,
        skipped=0,
        exclusions=(),
    )
    monkeypatch.setattr("kavita_ingest.wizard.detect_resume_state", lambda _: None)
    monkeypatch.setattr("kavita_ingest.wizard._preflight", lambda *args: None)
    monkeypatch.setattr("kavita_ingest.wizard.run_audit", lambda *args, **kwargs: audit)
    monkeypatch.setattr("kavita_ingest.wizard.interactive_review", lambda *args, **kwargs: audit)
    monkeypatch.setattr("kavita_ingest.wizard._incomplete_review_items", lambda *args, **kwargs: ())
    monkeypatch.setattr("kavita_ingest.wizard._create_plan", lambda *args: (stored, build))

    result = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nv\nt\na\ny\nq\n",
    )

    assert result.exit_code == 0, result.output
    assert "Start a new guided ingest?" not in result.output
    assert "Use configured incoming root" not in result.output
    assert "Ready to confirm     1" in result.output
    assert "Metadata and output" in result.output
    assert "Full SHA-256" in result.output
    assert "exact displayed plan digest is now locked" in result.output
    assert "Ingest complete" in result.output
    assert fixture.source.exists() and fixture.destination.exists()


def test_human_status_summarizes_last_ingest_and_next_action(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)

    result = CliRunner().invoke(
        app, ["status", "--config", str(_config(tmp_path, fixture))]
    )

    assert result.exit_code == 0, result.output
    assert "Last ingest" in result.output
    assert "1 item completed" in result.output
    assert "No recovery required" in result.output
    assert "Draft plans          0" in result.output
    assert "Approved plans       0" in result.output
    assert "Start a new guided ingest" in result.output


def test_disposable_fresh_wizard_smoke_publishes_with_safe_mode(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    source = incoming / "Watchmen 001.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.jpg", b"page-two")
    config = tmp_path / "smoke.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / 'state.sqlite3'}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[source]
lifecycle = "preserve"

[providers]
offline = true
[providers.open_library]
enabled = false
[providers.google_books]
enabled = false
[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\ni\nWatchmen\nAt Midnight\nissue\n1\n1986\nv\na\ny\nq\n",
    )

    destination = comics / "Watchmen (1986)" / "Watchmen (1986) - 001 - At Midnight.cbz"
    assert result.exit_code == 0, result.output
    assert "[1/7] Preflight" in result.output and "[7/7] Finish" in result.output
    assert "Metadata and output" in result.output and "Ingest complete" in result.output
    assert source.exists() and destination.exists()
    assert destination.stat().st_mode & 0o777 == 0o644

    second = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nq\n",
    )
    assert second.exit_code == 0, second.output
    assert "Already ingested    1" in second.output
    assert "Nothing new to ingest" in second.output
    assert "[N] New/change source" in second.output
    assert "[3/7] Review" not in second.output

    reprocess = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nr\nq\nq\n",
    )
    assert reprocess.exit_code == 0, reprocess.output
    assert "[3/7] Review" in reprocess.output
    assert "Review is incomplete" in reprocess.output
    assert destination.exists()


def test_review_completion_gate_blocks_missing_but_allows_explicit_unresolved(
    tmp_path: Path,
) -> None:
    from tests.test_review_ux import _audit

    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    settings = AppConfig(database_path=database)
    assert len(_incomplete_review_items(settings, audit)) == 1

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.UNRESOLVED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit) == ()

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit) == ()


def test_review_resume_filters_53_items_down_to_18_pending(tmp_path: Path) -> None:
    from tests.test_review_ux import _audit

    database = tmp_path / "state.sqlite3"
    migrate(database)
    base = _audit(tmp_path, eligible=True)
    items = []
    for index in range(53):
        source = replace(
            base.items[0].scan.source,
            path=tmp_path / f"Issue {index + 1:03}.cbz",
            sha256=f"{index + 100:064x}",
        )
        items.append(replace(base.items[0], scan=SimpleNamespace(source=source)))
    audit = replace(base, items=tuple(items), summary={"sources": 53})
    settings = AppConfig(database_path=database)

    with connect(database) as connection:
        repository = DecisionRepository(connection)
        for item in items[:35]:
            repository.add(
                item.scan.source,
                DecisionType.SKIPPED,
                item.local.evidence_hash(),
                payload={"explicit": True},
            )

    incomplete = _incomplete_review_items(settings, audit)

    assert len(incomplete) == 18
    assert [item.source.sha256 for item in incomplete] == [
        item.scan.source.sha256 for item in items[35:]
    ]


def test_changed_run_group_reopens_only_incompatible_current_decision(
    tmp_path: Path,
) -> None:
    from tests.test_review_ux import _audit

    database = tmp_path / "state.sqlite3"
    migrate(database)
    base = _audit(tmp_path, eligible=True)
    first_source = replace(
        base.items[0].scan.source,
        path=tmp_path / "Watchmen 001.cbz",
        sha256="1" * 64,
    )
    second_source = replace(
        base.items[0].scan.source,
        path=tmp_path / "Watchmen 002.cbz",
        sha256="2" * 64,
    )
    other_source = replace(
        base.items[0].scan.source,
        path=tmp_path / "Other 001.cbz",
        sha256="3" * 64,
    )
    first = replace(base.items[0], scan=SimpleNamespace(source=first_source))
    second = replace(base.items[0], scan=SimpleNamespace(source=second_source))
    other_local = replace(base.items[0].local, title="Other", series_title="Other")
    other = replace(
        base.items[0],
        scan=SimpleNamespace(source=other_source),
        local=other_local,
    )
    audit = replace(base, items=(first, second, other), summary={"sources": 3})
    selected_run_id = "4050-1987"

    with connect(database) as connection:
        repository = DecisionRepository(connection)
        repository.add(
            first.scan.source,
            DecisionType.ACCEPTED,
            first.local.evidence_hash(),
            payload={"candidate": first.scores[0].candidate.to_dict()},
        )
        selected_candidate = replace(first.scores[0].candidate, run_id=selected_run_id)
        repository.add(
            second.scan.source,
            DecisionType.ACCEPTED,
            second.local.evidence_hash(),
            payload={"candidate": selected_candidate.to_dict()},
        )
        repository.add(
            other.scan.source,
            DecisionType.ACCEPTED,
            other.local.evidence_hash(),
            payload={"candidate": other.scores[0].candidate.to_dict()},
        )
        assert (
            _mark_incompatible_group_decisions(
                repository, audit, run_group_key("Watchmen"), selected_run_id
            )
            == 1
        )

    incomplete = _incomplete_review_items(AppConfig(database_path=database), audit)

    assert [item.source.sha256 for item in incomplete] == [first.scan.source.sha256]


def test_select_root_reprompts_after_nonexistent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    answers = iter([str(tmp_path / "missing"), str(valid)])
    monkeypatch.setattr(
        "kavita_ingest.wizard.typer.prompt", lambda *args, **kwargs: next(answers)
    )

    assert _select_root(AppConfig()) == valid.resolve()


def test_completed_preserved_source_is_suppressed_only_with_matching_destination(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert not assessment.current
    assert len(assessment.completed) == 1
    assert assessment.completed[0].destination == fixture.destination


def test_changed_completed_source_is_current_work(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    with zipfile.ZipFile(fixture.source, "a") as archive:
        archive.writestr("003.jpg", b"changed-page")
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert len(assessment.current) == 1
    assert not assessment.completed and not assessment.warnings


@pytest.mark.parametrize("condition", ["missing", "mismatch"])
def test_completed_source_with_invalid_destination_returns_to_current_work(
    condition: str, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    if condition == "missing":
        fixture.destination.unlink()
    else:
        fixture.destination.write_bytes(b"changed destination")
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert len(assessment.current) == 1 and not assessment.completed
    assert assessment.warnings[0].condition == f"destination_{condition}"


@pytest.mark.parametrize("lifecycle", ["move_after_verify", "archive_after_verify"])
def test_completed_moved_or_archived_source_is_not_rediscovered(
    lifecycle: str, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle=lifecycle)
    ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert scan(fixture.source.parent, fixture.config, persist=True) == []


def test_explicit_reprocess_selection_returns_to_review_and_requires_new_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_review_ux import _audit

    audit = _audit(tmp_path, eligible=True)
    completed = SimpleNamespace(
        current=(),
        completed=(SimpleNamespace(scan=audit.items[0].scan, destination=tmp_path / "out.cbz"),),
        warnings=(),
    )
    monkeypatch.setattr("kavita_ingest.wizard._choice", lambda default: "R")

    selection = _select_discovered_sources(completed, Console(file=io.StringIO()))

    assert selection == DiscoverySelection((audit.items[0].scan,), reprocess=True)

    database = tmp_path / "state.sqlite3"
    migrate(database)
    settings = AppConfig(database_path=database)
    with connect(database) as connection:
        first = DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    baseline = {audit.items[0].scan.source.sha256: first.id}
    assert len(_incomplete_review_items(settings, audit, required_newer_than=baseline)) == 1

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit, required_newer_than=baseline) == ()


def test_plan_builder_excludes_missing_historical_source_after_rename(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"

    for directory in (incoming, books, comics):
        directory.mkdir()

    old_path = create_pdf(incoming / "Old Book Name.pdf")

    database = tmp_path / "state.sqlite3"
    settings = AppConfig(
        incoming_roots=(incoming,),
        books_root=books,
        comics_root=comics,
        database_path=database,
        source_lifecycle="preserve",
    )
    migrate(database)

    old_scan = scan(incoming, settings, persist=True)[0]

    with connect(database) as connection:
        add_manual_identity(
            DecisionRepository(connection),
            old_scan.source,
            "old-source-evidence",
            {
                "title": "Fixture Book",
                "authors": "Fixture Author",
            },
        )

    new_path = incoming / "New Book Name.pdf"
    old_path.rename(new_path)

    new_scan = scan(incoming, settings, persist=True)[0]

    with connect(database) as connection:
        add_manual_identity(
            DecisionRepository(connection),
            new_scan.source,
            "new-source-evidence",
            {
                "title": "Fixture Book",
                "authors": "Fixture Author",
            },
        )

        result = PlanBuilder(connection, settings).build(incoming)

    assert len(result.document.items) == 1
    assert result.document.items[0].source.path == str(new_path.resolve())

    assert any(
        exclusion.path == str(old_path.resolve(strict=False))
        and exclusion.category == "historical_missing"
        for exclusion in result.exclusions
    )
    assert result.unresolved_blocked == 0


def test_reprocess_plan_preserves_destination_no_clobber_conflict(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    scanned = scan(fixture.source.parent, fixture.config, persist=True)[0]
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        add_manual_identity(
            DecisionRepository(connection),
            scanned.source,
            "explicit-reprocess-evidence",
            {
                "series_title": "Watchmen",
                "title": "At Midnight",
                "item_type": "issue",
                "sequence": "1",
                "run_start_year": "1986",
            },
        )
        result = PlanBuilder(connection, fixture.config).build(fixture.source.parent)

    assert result.conflicts == 1
    assert result.document.items[0].conflicts[0].code == "destination_exists"
    assert fixture.destination.exists()
