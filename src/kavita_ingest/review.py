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
    batch_eligible_items,
)
from .domain import SourceRecord
from .matching import (
    CandidateScore,
    Reconciliation,
    reconcile,
    score_candidates,
    usable_identity_scores,
)
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
    counts = {
        key: 0 for key in ("accepted", "work_only", "manual", "rejected", "unresolved", "skipped")
    }
    try:
        for item in audit.items:
            current = item
            selected_rank = (
                _displayed_scores(current)[0].rank if _displayed_scores(current) else None
            )
            decided = False
            while True:
                _show_item(output, current, selected_rank)
                prompt = _action_prompt(current, audit)
                action = typer.prompt(prompt, default="N").strip().upper()
                displayed = _displayed_scores(current)
                selected = next((score for score in displayed if score.rank == selected_rank), None)
                if action.isdigit() and any(score.rank == int(action) for score in displayed):
                    selected_rank = int(action)
                    continue
                if action == "C" and displayed:
                    rank = typer.prompt("Candidate rank", type=int)
                    if any(score.rank == rank for score in displayed):
                        selected_rank = rank
                    else:
                        output.print(f"Candidate rank {rank} is not displayed.")
                    continue
                if action == "X" and selected:
                    output.print("\n".join(selected.explanation()))
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
                    displayed = _displayed_scores(current)
                    selected_rank = displayed[0].rank if displayed else None
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
                    fields = _manual_identity_fields(current)
                    add_manual_identity(
                        repository,
                        current.scan.source,
                        current.local.evidence_hash(),
                        fields,
                    )
                    output.print("Manual canonical identity explicitly approved.")
                    output.print("Decision saved.")
                    counts["manual"] += 1
                    decided = True
                    break
                if action == "G" and selected:
                    candidate = selected.candidate
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
                if action == "A" and selected:
                    if selected.candidate.record_type is RecordType.COMIC_RUN:
                        output.print("A run provides context only; select an issue candidate.")
                        continue
                    if not selected.eligible and not typer.confirm(
                        _low_confidence_message(selected)
                    ):
                        output.print("Not accepted; no decision was saved.")
                        continue
                    work_only = selected.candidate.record_type is RecordType.BOOK_WORK
                    if work_only and not typer.confirm(
                        "This candidate identifies only the work, not this EPUB edition. "
                        "Accept it work-only?"
                    ):
                        continue
                    accept_candidate(
                        repository,
                        current.scan.source,
                        selected,
                        reconcile(current.local, selected),
                        current.local.evidence_hash(),
                        work_only=work_only,
                    )
                    output.print("Decision saved.")
                    counts["work_only" if work_only else "accepted"] += 1
                    decided = True
                    break
                if action == "W" and selected and current.local.kind.value == "book":
                    accept_candidate(
                        repository,
                        current.scan.source,
                        selected,
                        reconcile(current.local, selected),
                        current.local.evidence_hash(),
                        work_only=True,
                    )
                    output.print("Decision saved.")
                    counts["work_only"] += 1
                    decided = True
                    break
                if action == "R" and selected:
                    repository.add(
                        current.scan.source,
                        DecisionType.REJECTED,
                        current.local.evidence_hash(),
                        candidate_key=selected.candidate.key,
                        candidate_data_hash=selected.candidate.data_hash(),
                        payload={"explicit": True},
                    )
                    output.print("Decision saved.")
                    counts["rejected"] += 1
                    decided = True
                    break
                if action in {"U", "K"}:
                    repository.add(
                        current.scan.source,
                        DecisionType.UNRESOLVED if action == "U" else DecisionType.SKIPPED,
                        current.local.evidence_hash(),
                        payload={"explicit": True},
                    )
                    output.print("Decision saved.")
                    counts["unresolved" if action == "U" else "skipped"] += 1
                    decided = True
                    break
                if action == "B":
                    eligible = _batch_items(audit)
                    output.print(
                        f"Eligible, non-conflicting, edition-resolved items: {len(eligible)}"
                    )
                    if typer.confirm(f"Explicitly accept all {len(eligible)} items?"):
                        batch_accept(repository, eligible, confirmed_count=len(eligible))
                        counts["accepted"] += len(eligible)
                        output.print(f"Decisions saved: {len(eligible)}.")
                    _show_summary(output, counts, len(audit.items))
                    return audit
                if action == "Q":
                    _show_summary(output, counts, len(audit.items))
                    return audit
                if action == "N":
                    break
            if not decided:
                continue
        _show_summary(output, counts, len(audit.items))
        return audit
    finally:
        connection.close()


def _batch_items(
    audit: AuditResult,
) -> list[tuple[SourceRecord, CandidateScore, Reconciliation, str]]:
    items = [
        (item.scan.source, item.scores[0], item.reconciliation, item.local.evidence_hash())
        for item in audit.items
        if item.scores
    ]
    return batch_eligible_items(items)


def _show_item(console: Console, item: ReviewItem, selected_rank: int | None) -> None:
    console.rule(item.scan.source.path.name)
    console.print(
        f"Path: {item.scan.source.path}\n"
        f"Classification: {item.local.kind.value}/{item.local.subtype} "
        f"({item.local.classification_confidence:.2f})"
    )
    if item.local.kind.value == "comic":
        console.print(
            f"Series: {item.local.series_title or item.local.title}\n"
            f"Issue: {item.local.sequence.normalized if item.local.sequence else 'unresolved'}\n"
            f"Publication year evidence: {item.local.year or 'unresolved'}\n"
            f"Run start: {item.local.run_start_year or 'unresolved'}"
        )
        table = Table(
            "",
            "Rank",
            "Issue",
            "Run context",
            "Publication",
            "Score",
            "Eligible",
        )
    else:
        console.print(f"Local title: {item.local.title}")
        table = Table("", "Rank", "Candidate", "Type", "Score", "Margin", "Eligible")
    displayed = _displayed_scores(item)
    for score in displayed:
        marker = ">" if score.rank == selected_rank else ""
        if item.local.kind.value == "comic":
            table.add_row(
                marker,
                str(score.rank),
                f"{score.candidate.series_title or '-'} "
                f"#{score.candidate.sequence.normalized if score.candidate.sequence else '-'}\n"
                f"{score.candidate.title}",
                f"start {score.candidate.run_start_year or '-'}\n{score.candidate.run_id or '-'}",
                f"{score.candidate.publication_date or '-'}\n{score.candidate.publisher or '-'}",
                f"{score.score:.1f}",
                "yes" if score.eligible else "no",
            )
            continue
        table.add_row(
            marker,
            str(score.rank),
            score.candidate.title,
            score.candidate.record_type.value,
            f"{score.score:.1f}",
            f"{score.runner_up_margin:.1f}",
            "yes" if score.eligible else "no",
        )
    console.print(table)
    if not displayed:
        console.print("No useful identity candidates are available.")
    if item.reconciliation.work_state:
        console.print(
            f"Work: {item.reconciliation.work_state}; Edition: {item.reconciliation.edition_state}"
        )
    if item.generation.unavailable:
        console.print("Unavailable: " + "; ".join(item.generation.unavailable))


def _displayed_scores(item: ReviewItem) -> list[CandidateScore]:
    return usable_identity_scores(item.scores)[:9]


def _action_prompt(item: ReviewItem, audit: AuditResult) -> str:
    displayed = _displayed_scores(item)
    actions = ["[N]ext source"]
    if len(_batch_items(audit)) > 0:
        actions.append("[B]atch")
    if displayed:
        actions.insert(0, "[A]ccept")
        actions.extend(["[R]eject", "[C]hoose candidate"])
    actions.extend(["[S]earch", "[E]dit", "[I]dentity"])
    if displayed:
        if any(score.candidate.run_id for score in displayed):
            actions.append("[G]roup-run")
        if item.local.kind.value == "book":
            actions.append("[W]ork-only")
        actions.append("e[X]plain")
    actions.extend(["[U]nresolved", "[K]skip", "[Q]uit"])
    return " ".join(actions)


def _low_confidence_message(score: CandidateScore) -> str:
    reason = (
        "several candidates remain tied"
        if score.runner_up_margin < 1
        else "the evidence does not meet the safe high-confidence threshold"
    )
    return (
        "This match is below the safe high-confidence threshold.\n\n"
        f"Score: {score.score:.1f}\nRunner-up margin: {score.runner_up_margin:.1f}\n"
        f"Reason: {reason}.\n\nAccept this specific candidate anyway?"
    )


def _show_summary(console: Console, counts: dict[str, int], total: int) -> None:
    decided = sum(counts.values())
    console.print("\nReview complete.\n")
    console.print(f"Accepted:    {counts['accepted']}")
    console.print(f"Work-only:   {counts['work_only']}")
    console.print(f"Manual:      {counts['manual']}")
    console.print(f"Rejected:    {counts['rejected']}")
    console.print(f"Unresolved:  {counts['unresolved']}")
    console.print(f"Skipped:     {counts['skipped']}")
    console.print(f"No decision: {max(total - decided, 0)}")
    console.print("\nNo media files were modified.")


def _manual_identity_fields(item: ReviewItem) -> dict[str, str]:
    local = item.local
    if local.kind.value == "book":
        fields = {
            "title": typer.prompt("Canonical title", default=local.title),
            "authors": typer.prompt(
                "Author(s), comma separated", default=", ".join(local.creators)
            ),
        }
        _optional_prompt(fields, "series_title", "Series")
        _optional_prompt(fields, "series_index", "Series index")
        _optional_prompt(fields, "publisher", "Publisher")
        _optional_prompt(fields, "publication_date", "Publication date (YYYY-MM-DD)")
        _optional_prompt(fields, "language", "Language tag")
        _optional_prompt(fields, "isbn", "ISBN")
        return fields
    fields = {
        "series_title": typer.prompt("Canonical series", default=local.series_title or local.title),
        "title": typer.prompt("Issue/collection title", default=local.title),
    }
    item_type = typer.prompt(
        "Comic item type",
        default=local.subtype
        if local.subtype
        in {
            "issue",
            "annual",
            "special",
            "one-shot",
            "trade",
            "collected-edition",
            "omnibus",
            "graphic-novel",
        }
        else "issue",
    )
    fields["item_type"] = item_type
    if item_type not in {"one-shot", "graphic-novel"}:
        fields["sequence"] = typer.prompt(
            "Sequence", default=local.sequence.raw if local.sequence else "1"
        )
    if item_type in {"issue", "annual", "special"}:
        fields["run_start_year"] = typer.prompt(
            "Run start year", default=str(local.run_start_year or "")
        )
    if item_type in {"trade", "collected-edition", "omnibus"}:
        _optional_prompt(fields, "collection_volume", "Integer collection volume")
    return fields


def _optional_prompt(fields: dict[str, str], key: str, label: str) -> None:
    value = typer.prompt(f"{label} (optional)", default="").strip()
    if value:
        fields[key] = value
