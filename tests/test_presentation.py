from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from kavita_ingest.db import connect
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.presentation import (
    render_plan_details,
    render_plan_summary,
    render_technical_plan,
)
from tests.apply_helpers import make_apply_fixture


@pytest.mark.parametrize("width", [80, 100, 132])
def test_plan_summary_is_stacked_width_safe_and_uses_relative_destination(
    width: int, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    stream = io.StringIO()
    render_plan_summary(
        plan, Console(file=stream, width=width, force_terminal=False), technical_header=False
    )
    text = stream.getvalue()
    assert "Watchmen (1986) #1" in text
    assert "At Midnight" in text
    assert "Watchmen (1986)/" in text
    assert str(fixture.destination) not in text
    assert max(len(line) for line in text.splitlines()) <= width


def test_book_summary_and_human_metadata_details_are_useful(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "epub", lifecycle="preserve")
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    stream = io.StringIO()
    console = Console(file=stream, width=80, force_terminal=False)
    render_plan_summary(plan, console, technical_header=False)
    render_plan_details(plan, console)
    text = stream.getvalue()
    assert "Resolved Book" in text and "Alex Author" in text
    assert "Resolved Book/" in text
    assert "Transformation" in text and "Metadata only" in text
    assert "Preserve unchanged" in text
    assert "File 0644" in text and "Directory 0755" in text
    assert "Conflicts" in text and "None" in text


def test_technical_view_exposes_exact_persisted_digest_and_document(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    stream = io.StringIO()
    render_technical_plan(plan, Console(file=stream, width=100, force_terminal=False))
    text = stream.getvalue()
    assert plan.sha256 in text
    assert '"planning_policy"' in text and '"schema_version": 2' in text
