from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel
from kavita_ingest.cli import app
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.planning import SourcePrecondition, build_snapshot, new_plan
from kavita_ingest.projection import project_comic
from kavita_ingest.providers.models import NormalizedCandidate, ProviderName, RecordType


def _config(tmp_path: Path, name: str = "state.sqlite3") -> Path:
    config = tmp_path / f"{name}.toml"
    config.write_text(f'[paths]\ndatabase = "{tmp_path / name}"\n', encoding="utf-8")
    return config


def _plan_file(tmp_path: Path) -> Path:
    identity = CanonicalIdentity(
        MediaKind.COMIC,
        "At Midnight",
        (),
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
        run_start_year=1986,
        item_type="issue",
        resolution=ResolutionLevel.MANUAL,
    )
    snapshot = build_snapshot(
        item_id="watchmen-1",
        source=SourcePrecondition("/incoming/watchmen.cbz", "a" * 64, 10, 20, "cbz"),
        identity=identity,
        projection=project_comic(identity),
        decision_provenance={
            "decision_type": "manual_identity",
            "decision_id": 1,
            "explicit_approval": True,
        },
        transformations=({"type": "metadata_only"},),
        writer_versions={"comicinfo_schema": "2.1"},
        expected_inventory=(),
        verification_requirements=("metadata_readback",),
    )
    path = tmp_path / "resolved.json"
    path.write_bytes(new_plan("cli-plan", (snapshot,)).canonical_bytes())
    return path


def test_plan_cli_import_show_approve_export_and_reimport_are_digest_bound(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    source = _plan_file(tmp_path)
    created = runner.invoke(app, ["plan", "import", str(source), "--config", str(config)])
    assert created.exit_code == 0, created.output
    match = re.search(r"plan (\d+) sha256=([0-9a-f]{64})", created.output)
    assert match
    plan_id, digest = match.groups()
    shown = runner.invoke(app, ["plan", "show", plan_id, "--config", str(config)])
    assert shown.exit_code == 0 and digest in shown.output and '"schema_version":2' in shown.output
    refused = runner.invoke(
        app, ["plan", "approve", plan_id, "--digest", "0" * 64, "--config", str(config)]
    )
    assert refused.exit_code != 0
    approved = runner.invoke(
        app, ["plan", "approve", plan_id, "--digest", digest, "--config", str(config)]
    )
    assert approved.exit_code == 0, approved.output
    exported = tmp_path / "export.json"
    result = runner.invoke(app, ["plan", "export", plan_id, str(exported), "--config", str(config)])
    assert result.exit_code == 0
    assert len(exported.read_bytes()) > 0

    second_config = _config(tmp_path, "import.sqlite3")
    imported = runner.invoke(app, ["plan", "import", str(exported), "--config", str(second_config)])
    assert imported.exit_code == 0 and "draft plan" in imported.output


def test_plan_list_and_expected_errors_do_not_leak_tracebacks(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    missing = runner.invoke(app, ["plan", "show", "99", "--config", str(config)])
    assert missing.exit_code == 2
    assert "REFUSED: 'plan 99 does not exist'." in missing.output
    assert "Traceback" not in missing.output
    approve = runner.invoke(
        app,
        ["plan", "approve", "99", "--digest", "0" * 64, "--config", str(config)],
    )
    assert approve.exit_code == 2
    assert "Traceback" not in approve.output
    status = runner.invoke(app, ["apply-status", "99", "--config", str(config)])
    assert status.exit_code == 2
    assert "Traceback" not in status.output
    empty = runner.invoke(app, ["plan", "list", "--config", str(config)])
    assert empty.exit_code == 0 and "No plans exist." in empty.output

    imported = runner.invoke(
        app, ["plan", "import", str(_plan_file(tmp_path)), "--config", str(config)]
    )
    assert imported.exit_code == 0
    listed = runner.invoke(app, ["plan", "list", "--config", str(config)])
    assert listed.exit_code == 0
    assert "draft" in listed.output and "Items" in listed.output

    mismatch = runner.invoke(
        app,
        ["plan", "approve", "1", "--digest", "0" * 64, "--config", str(config)],
    )
    assert mismatch.exit_code == 2
    assert "approval digest does not match" in mismatch.output
    assert "Traceback" not in mismatch.output


def test_run_group_cli_is_auditable_clearable_and_does_not_approve_items(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    run = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4050-123",
        RecordType.COMIC_RUN,
        MediaKind.COMIC,
        "Watchmen",
        series_title="Watchmen",
        run_start_year=1986,
        run_id="4050-123",
    )
    snapshot = tmp_path / "run.json"
    snapshot.write_text(json.dumps(run.to_dict(), default=str), encoding="utf-8")
    chosen = runner.invoke(
        app,
        [
            "run-group",
            "choose",
            "comic:watchmen",
            "4050-123",
            str(snapshot),
            "--config",
            str(config),
        ],
    )
    assert chosen.exit_code == 0 and "no issue identity was accepted" in chosen.output
    history = runner.invoke(
        app, ["run-group", "history", "comic:watchmen", "--config", str(config)]
    )
    assert history.exit_code == 0 and "4050-123" in history.output
    cleared = runner.invoke(app, ["run-group", "clear", "comic:watchmen", "--config", str(config)])
    assert cleared.exit_code == 0
