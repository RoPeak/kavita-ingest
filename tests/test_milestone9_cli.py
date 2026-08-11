from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kavita_ingest import __version__
from kavita_ingest.cli import OUTPUT_VERSION, app
from kavita_ingest.config import load_config
from kavita_ingest.db import connect
from kavita_ingest.plan_store import PlanStore
from tests.apply_helpers import ApplyFixture, make_apply_fixture


def _runtime_config(path: Path, fixture: ApplyFixture) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    config = path / "runtime.toml"
    config.write_text(
        f'''[paths]
database = "{path / 'workflow.sqlite3'}"
books = "{fixture.config.books_root}"
comics = "{fixture.config.comics_root}"
incoming = ["{fixture.source.parent}"]

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


def _export_fixture_plan(fixture: ApplyFixture, destination: Path) -> None:
    with connect(fixture.config.database_path) as connection:
        PlanStore(connection).export(fixture.plan_id, destination)


def _create_and_approve(runner: CliRunner, config: Path, plan: Path) -> tuple[str, str]:
    created = runner.invoke(app, ["plan", "import", str(plan), "--config", str(config)])
    assert created.exit_code == 0, created.output
    match = re.search(r"plan (\d+) sha256=([0-9a-f]{64})", created.output)
    assert match
    plan_id, digest = match.groups()
    approved = runner.invoke(
        app,
        ["plan", "approve", plan_id, "--digest", digest, "--config", str(config)],
    )
    assert approved.exit_code == 0, approved.output
    return plan_id, digest


def test_init_is_secret_free_and_refuses_overwrite(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    runner = CliRunner()
    created = runner.invoke(app, ["init", "--config", str(config)])
    assert created.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "COMIC_VINE_API_KEY" in text
    assert "GOOGLE_BOOKS_API_KEY" in text
    assert "api_key =" not in text.casefold()
    before = config.read_bytes()
    refused = runner.invoke(app, ["init", "--config", str(config)])
    assert refused.exit_code == 2
    assert config.read_bytes() == before
    assert load_config(config).source_lifecycle == "preserve"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            '[paths]\nbooks="/tmp/library"\ncomics="/tmp/library"\n',
            "destination roots must be different",
        ),
        (
            '[paths]\nincoming=["/tmp/reading"]\nbooks="/tmp/reading/books"\n',
            "nested inside incoming",
        ),
        (
            '[source]\nlifecycle="archive_after_verify"\n',
            "requires source.archive_root",
        ),
        ('[archive]\nmax_entries=0\n', "archive limits must be positive"),
        ('[matching]\neligible_score=101\n', "eligible_score must be between"),
        (
            '[providers.comic_vine]\nenabled=true\n',
            "explicitly enabled but COMIC_VINE_API_KEY",
        ),
    ],
)
def test_configuration_errors_are_actionable(
    body: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMIC_VINE_API_KEY", raising=False)
    config = tmp_path / "invalid.toml"
    config.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(config)


def test_version_help_default_menu_and_versioned_json(tmp_path: Path) -> None:
    runner = CliRunner()
    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0 and __version__ in version.output
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "apply" in help_result.output and "recover" in help_result.output
    menu = runner.invoke(app, [], input="q\n")
    assert menu.exit_code == 0
    assert "[1] Scan incoming media" in menu.output and "[Q] Quit" in menu.output

    config = tmp_path / "empty.toml"
    config.write_text(f'[paths]\ndatabase="{tmp_path / "state.sqlite3"}"\n', encoding="utf-8")
    status = runner.invoke(app, ["status", "--json", "--config", str(config)])
    payload = json.loads(status.stdout)
    assert payload["output_version"] == OUTPUT_VERSION
    assert payload["command"] == "status"

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    scanned = runner.invoke(
        app,
        ["scan", str(incoming), "--json", "--no-persist", "--config", str(config)],
    )
    assert json.loads(scanned.stdout)["command"] == "scan"
    audited = runner.invoke(
        app, ["audit", str(incoming), "--json", "--config", str(config)]
    )
    assert json.loads(audited.stdout)["command"] == "audit"


@pytest.mark.parametrize(
    ("media_format", "lifecycle", "work_only"),
    [
        ("epub", "preserve", False),
        ("epub", "preserve", True),
        ("cbr", "preserve", False),
        ("cbz", "preserve", False),
    ],
)
def test_real_cli_plan_approve_apply_and_status_workflow_is_disposable(
    media_format: str,
    lifecycle: str,
    work_only: bool,
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path / f"fixture-{media_format}-{work_only}",
        media_format,
        lifecycle=lifecycle,
        work_only=work_only,
        approve=False,
    )
    config = _runtime_config(tmp_path / f"runtime-{media_format}-{work_only}", fixture)
    plan_file = config.parent / "approved-plan.json"
    _export_fixture_plan(fixture, plan_file)
    runner = CliRunner()

    scanned = runner.invoke(
        app,
        ["scan", str(fixture.source.parent), "--config", str(config), "--no-persist"],
    )
    assert scanned.exit_code == 0 and "source files were not modified" in scanned.output
    plan_id, digest = _create_and_approve(runner, config, plan_file)
    shown = runner.invoke(
        app,
        ["plan", "show", plan_id, "--summary", "--json", "--config", str(config)],
    )
    assert json.loads(shown.stdout)["plan"]["sha256"] == digest
    applied = runner.invoke(app, ["apply", plan_id, "--json", "--config", str(config)])
    assert applied.exit_code == 0, applied.output
    result = json.loads(applied.stdout)
    assert result["summary"]["status"] == "complete"
    assert result["preview"]["lifecycle_counts"] == {"preserve": 1}
    assert fixture.source.exists() and fixture.destination.exists()
    status = runner.invoke(app, ["status", "--json", "--config", str(config)])
    assert json.loads(status.stdout)["counts"]["apply_runs"] == 1


def test_project_metadata_and_entry_point_are_release_ready() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    assert project["scripts"]["kavita-ingest"] == "kavita_ingest.cli:app"
    assert project["readme"] == "README.md"


def test_cli_apply_refuses_destination_collision_without_changing_either_file(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(
        tmp_path / "fixture-collision", "cbz", lifecycle="preserve", approve=False
    )
    config = _runtime_config(tmp_path / "runtime-collision", fixture)
    plan_file = config.parent / "collision-plan.json"
    _export_fixture_plan(fixture, plan_file)
    runner = CliRunner()
    plan_id, _ = _create_and_approve(runner, config, plan_file)
    source_before = fixture.source.read_bytes()
    fixture.destination.parent.mkdir(parents=True, exist_ok=True)
    fixture.destination.write_bytes(b"pre-existing-destination")

    refused = runner.invoke(app, ["apply", plan_id, "--config", str(config)])

    assert refused.exit_code == 2
    assert "destination now exists" in refused.output
    assert fixture.source.read_bytes() == source_before
    assert fixture.destination.read_bytes() == b"pre-existing-destination"
