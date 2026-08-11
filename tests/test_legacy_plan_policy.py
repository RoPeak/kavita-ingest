from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kavita_ingest.apply_engine import ApplyEngine, ApplyRefused
from kavita_ingest.cli import app
from kavita_ingest.db import connect
from kavita_ingest.plan_store import PlanStore, StoredPlan
from kavita_ingest.planning import LEGACY_POLICY_MESSAGE
from tests.apply_helpers import ApplyFixture, make_apply_fixture


def _legacy_plan(fixture: ApplyFixture, *, approved: bool = False) -> StoredPlan:
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        current = PlanStore(connection).get(fixture.plan_id)
        document = json.loads(current.canonical_json)
        document["plan_id"] = f"{document['plan_id']}-legacy"
        policy = dict(document["planning_policy"])
        policy["version"] = 1
        policy.pop("permissions", None)
        document["planning_policy"] = policy
        for item in document["items"]:
            item["planning_policy"] = policy
        payload = json.dumps(
            document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        legacy = PlanStore(connection).import_bytes(payload)
        if approved:
            connection.execute(
                "UPDATE plans SET status='approved', approved_at=?, approval_digest=? WHERE id=?",
                ("2026-01-01T00:00:00+00:00", legacy.sha256, legacy.id),
            )
            connection.commit()
            legacy = PlanStore(connection).get(legacy.id)
        return legacy


def _config(tmp_path: Path, fixture: ApplyFixture) -> Path:
    path = tmp_path / "legacy.toml"
    path.write_text(
        f'''[paths]
database = "{fixture.config.database_path}"
books = "{fixture.config.books_root}"
comics = "{fixture.config.comics_root}"
''',
        encoding="utf-8",
    )
    return path


def test_v1_plan_remains_viewable_but_draft_cannot_be_newly_approved(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    legacy = _legacy_plan(fixture)
    shown = CliRunner().invoke(
        app,
        [
            "plan",
            "show",
            str(legacy.id),
            "--summary",
            "--config",
            str(_config(tmp_path, fixture)),
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert "historical plan does not contain publication permissions" in shown.output

    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        with pytest.raises(ValueError, match="older planning-policy version"):
            PlanStore(connection).approve(legacy.id, legacy.sha256)
        invalidated = connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?", (legacy.id,)
        ).fetchone()
    assert invalidated and "Regenerate the plan" in invalidated["reason"]


def test_approved_unapplied_v1_plan_is_refused_without_invented_permissions(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    legacy = _legacy_plan(fixture, approved=True)

    with pytest.raises(ApplyRefused, match="Regenerate the plan"):
        ApplyEngine(fixture.config).preview(legacy.id)
    assert fixture.source.exists() and not fixture.destination.exists()


def test_completed_historical_v1_apply_remains_reportable(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    legacy = _legacy_plan(fixture, approved=True)
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO apply_runs(id, plan_id, plan_digest, status, started_at, updated_at, "
            "completed_at) VALUES ('historical', ?, ?, 'complete', ?, ?, ?)",
            (
                legacy.id,
                legacy.sha256,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO apply_items(run_id, item_id, state, source_path, "
            "planned_source_hash, destination_path, lifecycle_policy, started_at, updated_at, "
            "completed_at) VALUES "
            "('historical', 'item-1', 'complete', ?, ?, ?, 'preserve', ?, ?, ?)",
            (
                str(fixture.source),
                "a" * 64,
                str(fixture.destination),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00",
            ),
        )
        connection.commit()

    summary = ApplyEngine(fixture.config).status(legacy.id)
    assert summary is not None
    assert summary.status.value == "complete"
    assert summary.counts == {"complete": 1}


def test_regenerated_v2_plan_freezes_permissions_and_applies_normally(tmp_path: Path) -> None:
    fixture = make_apply_fixture(
        tmp_path,
        "cbz",
        lifecycle="preserve",
        file_mode=0o664,
        directory_mode=0o775,
    )
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        plan = PlanStore(connection).get(fixture.plan_id)
    document = json.loads(plan.canonical_json)
    assert document["planning_policy"]["version"] == 2
    assert document["planning_policy"]["permissions"] == {
        "file_mode": "0664",
        "directory_mode": "0775",
    }
    assert ApplyEngine(fixture.config).apply(fixture.plan_id).status.value == "complete"
    assert fixture.destination.stat().st_mode & 0o777 == 0o664


def test_legacy_refusal_message_is_actionable() -> None:
    assert "publication-permission policy" in LEGACY_POLICY_MESSAGE
    assert "Regenerate the plan" in LEGACY_POLICY_MESSAGE
