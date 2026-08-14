from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .apply_engine import ApplySummary
from .plan_store import StoredPlan


def plan_document(plan: StoredPlan) -> dict[str, Any]:
    parsed = json.loads(plan.canonical_json)
    if not isinstance(parsed, dict):
        raise ValueError("authoritative plan is not a JSON object")
    return parsed


def render_plan_summary(
    plan: StoredPlan,
    console: Console,
    *,
    technical_header: bool = True,
    pause_every: int | None = None,
) -> dict[str, Any]:
    document = plan_document(plan)
    items = _display_items(document)
    lifecycle = Counter(_lifecycle(item) for item in items)
    if technical_header:
        console.print(
            f"Plan {plan.id}  {plan.status.upper()}  {plan.sha256[:12]}...\n"
            f"Items: {len(items)}  Lifecycle: "
            + ", ".join(f"{key}={value}" for key, value in sorted(lifecycle.items()))
        )
    else:
        console.print(
            f"{len(items)} item{'s' if len(items) != 1 else ''}  "
            + ", ".join(_human_lifecycle(key) for key in sorted(lifecycle))
        )
    roots = sorted(
        {
            str(projection["library_root"])
            for item in items
            if isinstance((projection := item.get("kavita_projection")), dict)
            and projection.get("library_root")
        }
    )
    if roots:
        console.print("Library root" + ("s" if len(roots) > 1 else "") + ":")
        for root in roots:
            console.print(Text(f"  {root}"))
    for index, item in enumerate(items, start=1):
        console.print(_item_panel(item))
        if pause_every and index < len(items) and index % pause_every == 0:
            typer.prompt(
                f"Reviewed {index} of {len(items)} plan items. Press Enter to continue",
                default="",
                show_default=False,
            )
    policy = document.get("planning_policy", {})
    permissions = policy.get("permissions", {}) if isinstance(policy, dict) else {}
    if permissions:
        console.print(
            "Publication permissions: files "
            f"{permissions.get('file_mode')}, directories {permissions.get('directory_mode')}"
        )
    else:
        console.print(
            "Compatibility: this historical plan does not contain publication permissions; "
            "regenerate it before approval or apply."
        )
    console.print(f"Blocking conflicts: {len(_conflicts(document))}")
    return document


def render_plan_details(plan: StoredPlan, console: Console) -> dict[str, Any]:
    document = plan_document(plan)
    for item in _display_items(document):
        canonical = _mapping(item.get("canonical"))
        projection = _mapping(item.get("kavita_projection"))
        metadata = _mapping(projection.get("metadata"))
        lines = [_identity(item), ""]
        if canonical.get("media_kind") == "comic":
            _append_field(lines, "Series", metadata.get("Series"))
            for key in (
                "Writer",
                "Artist",
                "Penciller",
                "Inker",
                "Colorist",
                "Letterer",
                "CoverArtist",
                "Editor",
                "Translator",
            ):
                _append_field(lines, _metadata_label(key), metadata.get(key))
            _append_field(lines, "Publisher", metadata.get("Publisher"))
            _append_field(lines, "Release", _comic_date(metadata))
        else:
            _append_field(lines, "Authors", ", ".join(canonical.get("creators", [])))
            _append_field(lines, "Series", canonical.get("series_title"))
            _append_field(lines, "Publisher", canonical.get("publisher"))
            _append_field(lines, "Published", _friendly_date(canonical.get("publication_date")))
            _append_field(lines, "Language", canonical.get("language"))
        lines.extend(["", "Output", _relative_destination(item)])
        lines.extend(["", "Transformation", _transformations(item)])
        lines.extend(["", "Source", _human_lifecycle(_lifecycle(item))])
        permissions = _mapping(_mapping(document.get("planning_policy")).get("permissions"))
        if permissions:
            lines.extend(
                [
                    "",
                    "Permissions",
                    f"File {permissions.get('file_mode')}",
                    f"Directory {permissions.get('directory_mode')}",
                ]
            )
        conflicts = list(item.get("conflicts", []))
        lines.extend(["", "Conflicts", "None" if not conflicts else str(len(conflicts))])
        console.print(Panel(Text("\n".join(lines)), title="Metadata and output", expand=True))
    return document


def render_technical_plan(plan: StoredPlan, console: Console) -> None:
    console.print(f"Plan {plan.id}  Full SHA-256: {plan.sha256}")
    console.print_json(plan.canonical_json.decode("utf-8"))


def render_apply_summary(summary: ApplySummary, console: Console) -> None:
    console.print(f"Plan: {summary.plan_id}\nApply run: {summary.run_id}")
    console.print(f"Status: {summary.status.value}")
    for state, count in sorted(summary.counts.items()):
        console.print(f"  {state.replace('_', ' ').title():22} {count}")


def render_completed_apply(
    summary: ApplySummary,
    document: dict[str, Any],
    console: Console,
    *,
    compact: bool = False,
) -> None:
    completed = summary.counts.get("complete", 0)
    total = sum(summary.counts.values())
    lifecycles = {_lifecycle(item) for item in _items(document)}
    source_result = (
        "Source preserved"
        if lifecycles == {"preserve"}
        else "Planned source lifecycle completed"
    )
    progress = [
        f"✓ {completed} of {total} completed",
        "✓ Metadata verified",
        "✓ Destination verified",
        f"✓ {source_result}",
        "✓ No recovery required",
        "",
        "Published",
    ]
    items = _display_items(document)
    if compact and len(items) > 12:
        destinations = [
            PurePosixPath(str(_mapping(item.get("kavita_projection")).get("destination", "-")))
            for item in items
        ]
        groups = Counter(
            path.parent.as_posix() if len(path.parts) > 1 else "." for path in destinations
        )
        progress.extend(
            f"{count} item{'s' if count != 1 else ''} -> {folder}/"
            for folder, count in sorted(groups.items())
        )
        progress.extend(["", "Use [D] Details to list every published path."])
    else:
        progress.extend(_relative_destination(item) for item in items)
    console.print(Panel(Text("\n".join(progress)), title="Ingest complete", expand=True))


def render_human_status(
    connection: sqlite3.Connection,
    console: Console,
    *,
    next_action: str,
    details: bool = False,
) -> None:
    last = connection.execute(
        "SELECT id, status FROM apply_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    completed_items = 0
    if last:
        completed_items = int(
            connection.execute(
                "SELECT count(*) FROM apply_items WHERE run_id=? AND state='complete'",
                (last["id"],),
            ).fetchone()[0]
        )
    draft = _active_plan_count(connection, "draft")
    approved = _active_plan_count(connection, "approved")
    reviewed = int(
        connection.execute(
            "SELECT count(*) FROM decisions d WHERE d.id=(SELECT max(d2.id) FROM decisions d2 "
            "WHERE d2.source_fingerprint=d.source_fingerprint "
            "AND d2.media_signature=d.media_signature)"
        ).fetchone()[0]
    )
    lines = ["Kavita Ingest", ""]
    if last:
        lines.extend(
            [
                "Last ingest",
                (
                    f"✓ {completed_items} item{'s' if completed_items != 1 else ''} completed"
                    if last["status"] == "complete"
                    else f"! Recovery needed ({last['status'].replace('_', ' ')})"
                ),
                "✓ No recovery required" if last["status"] == "complete" else "",
                "",
            ]
        )
    lines.extend(
        [
            f"Reviewed items       {reviewed}",
            f"Draft plans          {draft}",
            f"Approved plans       {approved}",
            "",
            "Next",
            next_action,
        ]
    )
    if details:
        invalidated = int(
            connection.execute("SELECT count(*) FROM plan_invalidations").fetchone()[0]
        )
        superseded = int(
            connection.execute("SELECT count(*) FROM plan_supersessions").fetchone()[0]
        )
        lines.extend(
            ["", f"Invalidated plans    {invalidated}", f"Superseded plans     {superseded}"]
        )
    console.print(Text("\n".join(line for line in lines if line is not None)))


def _item_panel(item: dict[str, Any]) -> Panel:
    lines = [
        _identity(item),
        "",
        f"Source: {Path(str(_mapping(item.get('source')).get('path', '-'))).name}",
        "Output:",
        _relative_destination(item),
        f"Lifecycle: {_human_lifecycle(_lifecycle(item))}",
    ]
    return Panel(Text("\n".join(lines)), title="Planned item", expand=True)


def _identity(item: dict[str, Any]) -> str:
    canonical = _mapping(item.get("canonical"))
    projection = _mapping(item.get("kavita_projection"))
    metadata = _mapping(projection.get("metadata"))
    if canonical.get("media_kind") == "comic":
        series = metadata.get("Series") or canonical.get("series_title") or "Unresolved series"
        number = metadata.get("Number")
        heading = f"{series} #{number}" if number else str(series)
        title = canonical.get("title")
        return f"{heading}\n{title}" if title else heading
    title = str(canonical.get("title") or "Unresolved title")
    creators = canonical.get("creators", [])
    author = ", ".join(str(value) for value in creators)
    series = canonical.get("series_title")
    sequence = canonical.get("sequence")
    number = sequence.get("normalized") if isinstance(sequence, dict) else None
    context = f"{series} #{number}" if series and number else series
    details = [title, author, str(context or "")]
    return "\n".join(value for value in details if value)


def _relative_destination(item: dict[str, Any]) -> str:
    projection = _mapping(item.get("kavita_projection"))
    destination = PurePosixPath(str(projection.get("destination", "-")))
    if len(destination.parts) <= 1:
        return destination.as_posix()
    return f"{PurePosixPath(*destination.parts[:-1]).as_posix()}/\n{destination.name}"


def _transformations(item: dict[str, Any]) -> str:
    labels = {"metadata_only": "Metadata only", "cbr_to_cbz": "CBR to CBZ"}
    values = [
        labels.get(str(value.get("type")), str(value.get("type", "Unknown")))
        for value in item.get("transformations", [])
        if isinstance(value, dict)
    ]
    return ", ".join(values) or "None"


def _lifecycle(item: dict[str, Any]) -> str:
    actions = item.get("lifecycle_actions", [])
    if not actions:
        return "unknown"
    action = actions[-1].get("action", "unknown")
    return {
        "remove_source_after_verified_commit": "move_after_verify",
        "retain_source": "preserve",
    }.get(str(action), str(action))


def _human_lifecycle(value: str) -> str:
    return {
        "preserve": "Preserve unchanged",
        "move_after_verify": "Remove after verified publication",
        "archive_after_verify": "Archive after verified publication",
    }.get(value, value.replace("_", " ").title())


def _items(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("items", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _display_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(_items(document), key=_display_sort_key)


def _display_sort_key(item: dict[str, Any]) -> tuple[str, int, int, int, str, str]:
    canonical = _mapping(item.get("canonical"))
    series = str(canonical.get("series_title") or canonical.get("title") or "").casefold()
    sequence = canonical.get("sequence")
    if isinstance(sequence, dict) and isinstance(sequence.get("sort_key"), list):
        raw_key = sequence["sort_key"]
        if len(raw_key) == 3:
            try:
                return (
                    series,
                    0,
                    int(raw_key[0]),
                    int(raw_key[1]),
                    str(raw_key[2]),
                    _relative_destination(item).casefold(),
                )
            except (TypeError, ValueError):
                pass
    return (series, 1, 0, 0, "", _relative_destination(item).casefold())


def _conflicts(document: dict[str, Any]) -> list[object]:
    output = list(document.get("conflicts", []))
    output.extend(conflict for item in _items(document) for conflict in item.get("conflicts", []))
    return output


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _append_field(lines: list[str], label: str, value: object) -> None:
    if value not in (None, "", [], {}):
        lines.append(f"{label:<13} {value}")


def _metadata_label(value: str) -> str:
    return {"CoverArtist": "Cover artists"}.get(value, value)


def _comic_date(metadata: dict[str, Any]) -> str | None:
    try:
        value = date(int(metadata["Year"]), int(metadata["Month"]), int(metadata["Day"]))
    except (KeyError, TypeError, ValueError):
        return None
    return value.strftime("%d %b %Y")


def _friendly_date(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return value


def _active_plan_count(connection: sqlite3.Connection, status: str) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM plans p WHERE p.status=? "
            "AND NOT EXISTS (SELECT 1 FROM plan_invalidations x WHERE x.plan_id=p.id) "
            "AND NOT EXISTS (SELECT 1 FROM plan_supersessions s WHERE s.old_plan_id=p.id) "
            "AND NOT EXISTS (SELECT 1 FROM apply_runs a WHERE a.plan_id=p.id)",
            (status,),
        ).fetchone()[0]
    )
