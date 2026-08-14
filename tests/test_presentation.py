from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from kavita_ingest.apply_engine import ApplySummary
from kavita_ingest.apply_journal import RunState
from kavita_ingest.db import connect
from kavita_ingest.plan_store import PlanStore, StoredPlan
from kavita_ingest.presentation import (
    render_completed_apply,
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
    assert '"planning_policy"' in text and '"schema_version": 3' in text


def _display_plan_with_sequences(values: list[int]) -> StoredPlan:
    items = []
    for number in values:
        items.append(
            {
                "canonical": {
                    "media_kind": "comic",
                    "series_title": "Watchmen",
                    "title": f"Issue {number}",
                    "sequence": {
                        "raw": str(number),
                        "normalized": str(number),
                        "kind": "integer",
                        "sort_key": [0, number, ""],
                        "width": len(str(number)),
                    },
                },
                "source": {"path": f"/incoming/Watchmen #{number}.pdf"},
                "kavita_projection": {
                    "destination": f"Watchmen (1986)/Watchmen (1986) - {number:03d}.pdf",
                    "metadata": {"Series": "Watchmen (1986)", "Number": str(number)},
                },
                "lifecycle_actions": [{"action": "retain_source"}],
                "conflicts": [],
            }
        )
    document = {
        "schema_version": 3,
        "items": items,
        "conflicts": [],
        "planning_policy": {},
    }
    payload = json.dumps(document).encode()
    return StoredPlan(
        1,
        3,
        payload,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "draft",
        "2026-08-14T00:00:00+00:00",
        None,
        None,
    )


def test_plan_human_display_naturally_orders_issue_sequences() -> None:
    plan = _display_plan_with_sequences([1, 10, 11, 12, 2, 3])
    stream = io.StringIO()

    render_plan_summary(
        plan,
        Console(file=stream, width=100, force_terminal=False),
        technical_header=False,
    )

    text = stream.getvalue()
    positions = [text.index(f"Watchmen (1986) #{number}") for number in (1, 2, 3, 10, 11, 12)]
    assert positions == sorted(positions)


def test_compact_completed_apply_summarizes_large_batch() -> None:
    plan = _display_plan_with_sequences(list(range(1, 14)))
    document = json.loads(plan.canonical_json)
    stream = io.StringIO()

    render_completed_apply(
        ApplySummary("run", 1, RunState.COMPLETE, {"complete": 13}),
        document,
        Console(file=stream, width=100, force_terminal=False),
        compact=True,
    )

    text = stream.getvalue()
    assert "13 items -> Watchmen (1986)/" in text
    assert "Use [D] Details to list every published path." in text
    assert "Watchmen (1986) - 013.pdf" not in text
