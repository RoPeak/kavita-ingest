from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kavita_ingest.apply_engine import ApplyEngine, InjectedCrash, WriterDispatcher
from kavita_ingest.cli import app
from kavita_ingest.locking import ProcessLock, lock_path
from tests.apply_helpers import make_apply_fixture


def _config(path: Path, fixture) -> Path:  # type: ignore[no-untyped-def]
    config = path / "apply.toml"
    config.write_text(
        f"""
[paths]
database = "{fixture.config.database_path}"
books = "{fixture.config.books_root}"
comics = "{fixture.config.comics_root}"
""",
        encoding="utf-8",
    )
    return config


def test_apply_status_and_rollback_preview_cli_use_approved_synthetic_plan(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    config = _config(tmp_path, fixture)
    runner = CliRunner()
    applied = runner.invoke(app, ["apply", str(fixture.plan_id), "--config", str(config)])
    assert applied.exit_code == 0, applied.output
    assert "Status: complete" in applied.output
    status = runner.invoke(
        app, ["apply-status", str(fixture.plan_id), "--details", "--config", str(config)]
    )
    assert status.exit_code == 0
    assert "state=complete" in status.output and "proposed: none" in status.output
    rollback = runner.invoke(app, ["rollback", str(fixture.plan_id), "--config", str(config)])
    assert rollback.exit_code == 0
    assert "REVERSIBLE" in rollback.output
    assert "Preview only" in rollback.output
    assert fixture.destination.exists() and fixture.source.exists()


def test_recovery_cli_explains_and_recovers_crash_after_commit(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    config = _config(tmp_path, fixture)

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == "after_destination_commit":
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    runner = CliRunner()
    status = runner.invoke(
        app, ["apply-status", str(fixture.plan_id), "--details", "--config", str(config)]
    )
    assert status.exit_code == 0
    assert "state=committing" in status.output
    assert "recognize verified destination commit" in status.output
    recovered = runner.invoke(app, ["recover", str(fixture.plan_id), "--config", str(config)])
    assert recovered.exit_code == 0, recovered.output
    assert "Status: complete" in recovered.output
    repeated = runner.invoke(
        app,
        ["recover", str(fixture.plan_id), "--json", "--config", str(config)],
    )
    assert repeated.exit_code == 0
    assert json.loads(repeated.stdout)["summary"]["status"] == "complete"


def test_cli_refuses_unapproved_plan_without_convenience_override(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", approve=False)
    config = _config(tmp_path, fixture)
    result = CliRunner().invoke(app, ["apply", str(fixture.plan_id), "--config", str(config)])
    assert result.exit_code == 2
    assert "not explicitly approved" in result.output


def test_doctor_reports_recovery_required_apply_state(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    config = _config(tmp_path, fixture)

    def crash(name: str, item_id: str) -> None:
        del item_id
        if name == "after_verified_journal":
            raise InjectedCrash(name)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0
    assert "apply-state" in result.output
    assert "active/recoverable" in result.output


class _CliFailingWriter(WriterDispatcher):
    def stage(self, item, destination):  # type: ignore[no-untyped-def]
        del item, destination
        raise OSError("synthetic CLI abandonment failure")


def test_abandon_cli_closes_recoverable_run_and_doctor_becomes_clear(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    config = _config(tmp_path, fixture)

    first = ApplyEngine(
        fixture.config,
        writers=_CliFailingWriter(),
    ).apply(fixture.plan_id)

    assert first.status.value == "recovery_required"

    runner = CliRunner()

    abandoned = runner.invoke(
        app,
        [
            "abandon",
            str(fixture.plan_id),
            "--reason",
            "CLI regression restart",
            "--yes",
            "--config",
            str(config),
        ],
    )

    assert abandoned.exit_code == 0, abandoned.output
    assert "Journal history preserved" in abandoned.output
    assert fixture.source.exists()
    assert not fixture.destination.exists()

    status = runner.invoke(
        app,
        [
            "apply-status",
            str(fixture.plan_id),
            "--details",
            "--config",
            str(config),
        ],
    )

    assert status.exit_code == 0, status.output
    assert "Status: failed" in status.output
    assert "plan is invalidated" in status.output

    doctor = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(config),
        ],
    )

    assert doctor.exit_code == 0, doctor.output
    assert (
        "0 active/recoverable run(s); "
        "0 item(s) require attention"
        in doctor.output
    )


def test_apply_cli_reports_process_lock_contention_as_refusal(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")
    config = _config(tmp_path, fixture)
    database = fixture.config.database_path
    assert database is not None
    with ProcessLock(lock_path(database)):
        result = CliRunner().invoke(
            app, ["apply", str(fixture.plan_id), "--config", str(config)]
        )
    assert result.exit_code == 2
    assert "another apply, recovery, or rollback process" in result.output
