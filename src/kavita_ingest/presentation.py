from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .apply_engine import ApplySummary
from .plan_store import StoredPlan


def render_plan_summary(plan: StoredPlan, console: Console) -> dict[str, Any]:
    parsed = json.loads(plan.canonical_json)
    if not isinstance(parsed, dict):
        raise ValueError("authoritative plan is not a JSON object")
    document: dict[str, Any] = parsed
    items = document["items"]
    lifecycle = Counter(_lifecycle(item) for item in items)
    table = Table("Source", "Identity", "Destination", "Lifecycle")
    for item in items:
        canonical = item.get("canonical", {})
        projection = item.get("kavita_projection", {})
        identity = canonical.get("series_title") or canonical.get("title") or "unresolved"
        number = projection.get("number") if isinstance(projection, dict) else None
        if number:
            identity = f"{identity} #{number}"
        table.add_row(
            Path(str(item["source"]["path"])).name,
            str(identity),
            str(projection.get("absolute_destination") or projection.get("destination") or "-"),
            _lifecycle(item),
        )
    console.print(
        f"Plan {plan.id}  {plan.status.upper()}  {plan.sha256[:12]}...\n"
        f"Items: {len(items)}  Lifecycle: "
        + ", ".join(f"{key}={value}" for key, value in sorted(lifecycle.items()))
    )
    console.print(table)
    policy = document.get("planning_policy", {})
    permissions = policy.get("permissions", {}) if isinstance(policy, dict) else {}
    if permissions:
        console.print(
            "Publication permissions: files "
            f"{permissions.get('file_mode')}, directories {permissions.get('directory_mode')}"
        )
    conflicts = list(document.get("conflicts", []))
    conflicts.extend(
        conflict for item in items for conflict in item.get("conflicts", [])
    )
    console.print(f"Blocking conflicts: {len(conflicts)}")
    return document


def render_apply_summary(summary: ApplySummary, console: Console) -> None:
    console.print(f"Plan {summary.plan_id}  Apply run {summary.run_id}")
    console.print(f"Status: {summary.status.value}")
    for state, count in sorted(summary.counts.items()):
        console.print(f"  {state.replace('_', ' ').title():22} {count}")


def _lifecycle(item: dict[str, Any]) -> str:
    actions = item.get("lifecycle_actions", [])
    if not actions:
        return "unknown"
    action = actions[-1].get("action", "unknown")
    return {
        "remove_source_after_verified_commit": "move_after_verify",
        "retain_source": "preserve",
    }.get(str(action), str(action))
