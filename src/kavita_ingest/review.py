from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .audit import AuditResult, ReviewItem, run_audit
from .candidates import generate_candidates
from .config import AppConfig
from .db import connect
from .decisions import (
    DecisionRepository,
    DecisionType,
    accept_candidate,
    add_manual_identity,
    add_manual_override,
    batch_accept,
)
from .domain import SourceRecord
from .matching import CandidateScore, Reconciliation, reconcile, score_candidates
from .provider_runtime import build_providers
from .providers.models import NormalizedCandidate, ProviderName, RecordType
from .run_groups import RunGroupRepository, run_group_key


def interactive_review(
    root: Path, config: AppConfig, console: Console | None = None
) -> AuditResult:
    output = console or Console()
    audit = run_audit(root, config, mode="review")
    if config.database_path is None:
        raise ValueError("review requires a state database")
    connection = connect(config.database_path)
    repository = DecisionRepository(connection)
    run_groups = RunGroupRepository(connection)
    providers = build_providers(connection, config.providers)
    try:
        for item in audit.items:
            current = item
            while True:
                _show_item(output, current)
                action = (
                    typer.prompt(
                        "[A]ccept [N]ext [B]atch [R]eject [S]earch [E]dit "
                        "[I]dentity [G]roup-run [W]ork-only [U]nresolved [K]skip "
                        "e[X]plain [Q]uit",
                        default="N",
                    )
                    .strip()
                    .upper()
                )
                top = current.scores[0] if current.scores else None
                if action == "X" and top:
                    output.print("\n".join(top.explanation()))
                    continue
                if action == "S":
                    query = typer.prompt(
                        "Revised title/series search",
                        default=current.local.series_title or current.local.title,
                    )
                    local = (
                        replace(current.local, series_title=query)
                        if current.local.kind.value == "comic"
                        else replace(current.local, title=query)
                    )
                    generated = generate_candidates(local, providers)
                    scores = score_candidates(local, list(generated.candidates), config.matching)
                    scores = [
                        replace(
                            score,
                            suppressed=repository.rejection_suppresses(
                                current.scan.source,
                                score.candidate.key,
                                local.evidence_hash(),
                                score.candidate.data_hash(),
                            ),
                            eligible=score.eligible
                            and not repository.rejection_suppresses(
                                current.scan.source,
                                score.candidate.key,
                                local.evidence_hash(),
                                score.candidate.data_hash(),
                            ),
                        )
                        for score in scores
                    ]
                    current = ReviewItem(
                        current.scan,
                        local,
                        generated,
                        tuple(scores),
                        reconcile(local, scores[0] if scores else None),
                    )
                    continue
                if action == "E":
                    field = typer.prompt("Canonical field")
                    value = typer.prompt("Value")
                    add_manual_override(
                        repository,
                        current.scan.source,
                        current.local.evidence_hash(),
                        field,
                        value,
                    )
                    output.print("Manual override recorded with user provenance.")
                    continue
                if action == "I":
                    title_field = "series_title" if current.local.kind.value == "comic" else "title"
                    title = typer.prompt(
                        "Canonical title",
                        default=current.local.series_title or current.local.title,
                    )
                    fields = {title_field: title}
                    if current.local.kind.value == "comic":
                        fields["item_type"] = typer.prompt(
                            "Comic item type", default=current.local.subtype
                        )
                        if current.local.sequence:
                            fields["sequence"] = typer.prompt(
                                "Sequence", default=current.local.sequence.raw
                            )
                    add_manual_identity(
                        repository,
                        current.scan.source,
                        current.local.evidence_hash(),
                        fields,
                    )
                    output.print("Manual canonical identity explicitly approved.")
                    break
                if action == "G" and top:
                    candidate = top.candidate
                    if (
                        candidate.provider is not ProviderName.COMIC_VINE
                        or not candidate.run_id
                        or not candidate.series_title
                    ):
                        output.print("The displayed candidate has no selectable Comic Vine run.")
                        continue
                    run = NormalizedCandidate(
                        provider=ProviderName.COMIC_VINE,
                        provider_id=candidate.run_id,
                        record_type=RecordType.COMIC_RUN,
                        media_kind=candidate.media_kind,
                        title=candidate.series_title,
                        series_title=candidate.series_title,
                        run_start_year=candidate.run_start_year,
                        run_id=candidate.run_id,
                    )
                    if typer.confirm(
                        f"Use run {run.title} ({run.run_start_year or 'year unknown'}) "
                        "for this local series group?"
                    ):
                        run_groups.choose(
                            run_group_key(candidate.series_title),
                            ProviderName.COMIC_VINE.value,
                            candidate.run_id,
                            run.to_dict(),
                        )
                        output.print(
                            "Run-group choice recorded. Re-run review to constrain the group; "
                            "this issue was not accepted."
                        )
                        break
                    continue
                if action == "A" and top:
                    if not top.eligible and not typer.confirm(
                        "Candidate is not batch-eligible. Accept anyway?"
                    ):
                        continue
                    accept_candidate(
                        repository,
                        current.scan.source,
                        top,
                        current.reconciliation,
                        current.local.evidence_hash(),
                    )
                    break
                if action == "W" and top:
                    accept_candidate(
                        repository,
                        current.scan.source,
                        top,
                        current.reconciliation,
                        current.local.evidence_hash(),
                        work_only=True,
                    )
                    break
                if action == "R" and top:
                    repository.add(
                        current.scan.source,
                        DecisionType.REJECTED,
                        current.local.evidence_hash(),
                        candidate_key=top.candidate.key,
                        candidate_data_hash=top.candidate.data_hash(),
                        payload={"explicit": True},
                    )
                    break
                if action in {"U", "K"}:
                    repository.add(
                        current.scan.source,
                        DecisionType.UNRESOLVED if action == "U" else DecisionType.SKIPPED,
                        current.local.evidence_hash(),
                        payload={"explicit": True},
                    )
                    break
                if action == "B":
                    eligible = _batch_items(audit)
                    output.print(
                        f"Eligible, non-conflicting, edition-resolved items: {len(eligible)}"
                    )
                    if typer.confirm(f"Explicitly accept all {len(eligible)} items?"):
                        batch_accept(repository, eligible, confirmed_count=len(eligible))
                    return audit
                if action == "Q":
                    return audit
                if action == "N":
                    break
        return audit
    finally:
        connection.close()


def _batch_items(
    audit: AuditResult,
) -> list[tuple[SourceRecord, CandidateScore, Reconciliation, str]]:
    return [
        (item.scan.source, item.scores[0], item.reconciliation, item.local.evidence_hash())
        for item in audit.items
        if item.scores
    ]


def _show_item(console: Console, item: ReviewItem) -> None:
    console.rule(item.scan.source.path.name)
    console.print(
        f"Path: {item.scan.source.path}\n"
        f"Local: {item.local.series_title or item.local.title} "
        f"{item.local.sequence.normalized if item.local.sequence else ''}\n"
        f"Classification: {item.local.kind.value}/{item.local.subtype} "
        f"({item.local.classification_confidence:.2f})"
    )
    table = Table("Rank", "Candidate", "Type", "Score", "Margin", "Eligible")
    for score in item.scores[:5]:
        table.add_row(
            str(score.rank),
            score.candidate.title,
            score.candidate.record_type.value,
            f"{score.score:.1f}",
            f"{score.runner_up_margin:.1f}",
            "yes" if score.eligible else "no",
        )
    console.print(table)
    if item.reconciliation.work_state:
        console.print(
            f"Work: {item.reconciliation.work_state}; Edition: {item.reconciliation.edition_state}"
        )
    if item.generation.unavailable:
        console.print("Unavailable: " + "; ".join(item.generation.unavailable))
