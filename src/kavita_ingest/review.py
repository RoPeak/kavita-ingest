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
from .hydration import HydrationResult, hydrate_candidate
from .matching import (
    CandidateScore,
    LocalIdentity,
    Reconciliation,
    reconcile,
    score_candidates,
    usable_identity_scores,
)
from .provider_runtime import build_providers
from .providers.base import Provider, ProviderError
from .providers.comic_vine import ComicVineProvider
from .providers.models import NormalizedCandidate, ProviderName, RecordType, SearchQuery
from .run_groups import RunGroupRepository, run_group_key


def interactive_review(
    root: Path,
    config: AppConfig,
    console: Console | None = None,
    *,
    audit_result: AuditResult | None = None,
    wizard_mode: bool = False,
) -> AuditResult:
    output = console or Console()
    audit = audit_result or run_audit(root, config, mode="review")
    if config.database_path is None:
        raise ValueError("review requires a state database")
    connection = connect(config.database_path)
    repository = DecisionRepository(connection)
    run_groups = RunGroupRepository(connection)
    providers = build_providers(connection, config.providers)
    counts = {
        key: 0 for key in ("accepted", "work_only", "manual", "rejected", "unresolved", "skipped")
    }
    decided_this_pass: set[str] = set()
    restart_requested = False
    try:
        for item in audit.items:
            current = item
            source_evidence_hash = item.local.evidence_hash()
            advanced = not wizard_mode
            selected_rank = (
                _displayed_scores(current)[0].rank if _displayed_scores(current) else None
            )
            decided = False
            while True:
                _show_item(output, current, selected_rank, compact=wizard_mode and not advanced)
                prompt = _action_prompt(current, audit, wizard_mode=wizard_mode and not advanced)
                if wizard_mode and not advanced:
                    action = str(
                        typer.prompt(prompt, default=None, show_default=False)
                    ).strip().upper()
                else:
                    action = str(typer.prompt(prompt, default="N")).strip().upper()
                displayed = _displayed_scores(current)
                selected = next((score for score in displayed if score.rank == selected_rank), None)
                if action == "M" and wizard_mode:
                    advanced = True
                    output.print("Advanced review actions are now available for this item.")
                    continue
                if action == "V":
                    action = "X"
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
                    if (
                        current.local.kind.value == "comic"
                        and current.local.subtype == "collected-edition"
                    ):
                        query = typer.prompt(
                            "Revised collection title search",
                            default=_collection_search_text(current.local),
                        )
                        creator_text = typer.prompt(
                            "Creator search (optional)",
                            default=", ".join(current.local.creators),
                            show_default=bool(current.local.creators),
                        ).strip()
                        creators = tuple(
                            part.strip() for part in creator_text.split(",") if part.strip()
                        )
                        search_local = replace(
                            current.local,
                            title=query,
                            creators=creators,
                        )
                    else:
                        query = typer.prompt(
                            "Revised provider search",
                            default=current.local.series_title or current.local.title,
                        )
                        search_local = (
                            replace(
                                current.local,
                                title=query,
                                series_title=query,
                            )
                            if current.local.kind.value == "comic"
                            else replace(current.local, title=query)
                        )
                    generated = generate_candidates(search_local, providers)
                    scores = score_candidates(
                        current.local, list(generated.candidates), config.matching
                    )
                    scores = [
                        replace(
                            score,
                            suppressed=repository.rejection_suppresses(
                                current.scan.source,
                                score.candidate.key,
                                source_evidence_hash,
                                score.candidate.data_hash(),
                            ),
                            eligible=score.eligible
                            and not repository.rejection_suppresses(
                                current.scan.source,
                                score.candidate.key,
                                source_evidence_hash,
                                score.candidate.data_hash(),
                            ),
                        )
                        for score in scores
                    ]
                    current = ReviewItem(
                        current.scan,
                        current.local,
                        generated,
                        tuple(scores),
                        reconcile(current.local, scores[0] if scores else None),
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
                        source_evidence_hash,
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
                        source_evidence_hash,
                        fields,
                    )
                    output.print("Manual canonical identity explicitly approved.")
                    output.print("Decision saved.")
                    counts["manual"] += 1
                    decided_this_pass.add(current.scan.source.sha256)
                    decided = True
                    break
                if action == "G":
                    run = _choose_comic_vine_run(current, providers, output)
                    if run is None:
                        continue
                    assert run.series_title is not None
                    group_key = run_group_key(run.series_title)
                    if typer.confirm(
                        f"Use run {run.title} ({run.run_start_year}) "
                        "for this local series group?"
                    ):
                        run_groups.choose(
                            group_key,
                            ProviderName.COMIC_VINE.value,
                            run.provider_id,
                            run.to_dict(),
                        )
                        invalidated = _mark_incompatible_group_decisions(
                            repository, audit, group_key, run.provider_id
                        )
                        output.print(
                            "Run-group choice recorded. Review must restart so every issue is "
                            "matched inside that exact run."
                        )
                        if invalidated:
                            output.print(
                                f"Marked {invalidated} incompatible earlier issue "
                                "decision(s) unresolved for explicit re-review."
                            )
                        restart_requested = True
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
                    hydrated = _hydrate_for_acceptance(selected, providers, output)
                    if hydrated is None:
                        continue
                    selected, hydration = hydrated
                    accept_candidate(
                        repository,
                        current.scan.source,
                        selected,
                        reconcile(current.local, selected),
                        source_evidence_hash,
                        work_only=work_only,
                        hydration=hydration.to_dict(),
                    )
                    output.print("Decision saved.")
                    counts["work_only" if work_only else "accepted"] += 1
                    decided_this_pass.add(current.scan.source.sha256)
                    decided = True
                    break
                if action == "W" and selected and current.local.kind.value == "book":
                    hydrated = _hydrate_for_acceptance(selected, providers, output)
                    if hydrated is None:
                        continue
                    selected, hydration = hydrated
                    accept_candidate(
                        repository,
                        current.scan.source,
                        selected,
                        reconcile(current.local, selected),
                        source_evidence_hash,
                        work_only=True,
                        hydration=hydration.to_dict(),
                    )
                    output.print("Decision saved.")
                    counts["work_only"] += 1
                    decided_this_pass.add(current.scan.source.sha256)
                    decided = True
                    break
                if action == "R" and selected:
                    repository.add(
                        current.scan.source,
                        DecisionType.REJECTED,
                        source_evidence_hash,
                        candidate_key=selected.candidate.key,
                        candidate_data_hash=selected.candidate.data_hash(),
                        payload={"explicit": True},
                    )
                    output.print("Decision saved.")
                    counts["rejected"] += 1
                    decided_this_pass.add(current.scan.source.sha256)
                    decided = True
                    break
                if action in {"U", "K"}:
                    repository.add(
                        current.scan.source,
                        DecisionType.UNRESOLVED if action == "U" else DecisionType.SKIPPED,
                        source_evidence_hash,
                        payload={"explicit": True},
                    )
                    output.print("Decision saved.")
                    counts["unresolved" if action == "U" else "skipped"] += 1
                    decided_this_pass.add(current.scan.source.sha256)
                    decided = True
                    break
                if action == "B":
                    eligible = _batch_items(audit, exclude_fingerprints=decided_this_pass)
                    output.print(
                        f"Eligible, non-conflicting, edition-resolved items: {len(eligible)}"
                    )
                    hydrated_items: list[
                        tuple[SourceRecord, CandidateScore, Reconciliation, str]
                    ] = []
                    hydration_records: dict[str, dict[str, object]] = {}
                    for source, score, reconciliation, evidence_hash in eligible:
                        hydrated = _hydrate_for_acceptance(score, providers, output)
                        if hydrated is None:
                            output.print("Batch cancelled; no batch decisions were saved.")
                            hydrated_items = []
                            break
                        hydrated_score, hydration = hydrated
                        hydrated_items.append(
                            (
                                source,
                                hydrated_score,
                                reconciliation,
                                evidence_hash,
                            )
                        )
                        hydration_records[hydrated_score.candidate.key] = hydration.to_dict()
                    if hydrated_items and typer.confirm(
                        f"Explicitly accept all {len(hydrated_items)} enriched items?"
                    ):
                        batch_accept(
                            repository,
                            hydrated_items,
                            confirmed_count=len(hydrated_items),
                            hydration=hydration_records,
                        )
                        counts["accepted"] += len(hydrated_items)
                        output.print(f"Decisions saved: {len(hydrated_items)}.")
                    _show_summary(
                        output,
                        counts,
                        len(audit.items),
                        repository=repository,
                        audit=audit,
                    )
                    return audit
                if action == "Q":
                    _show_summary(
                        output,
                        counts,
                        len(audit.items),
                        repository=repository,
                        audit=audit,
                    )
                    return audit
                if action == "N":
                    break
            if restart_requested:
                break
            if not decided:
                continue
        _show_summary(output, counts, len(audit.items), repository=repository, audit=audit)
        return audit
    finally:
        connection.close()


def _batch_items(
    audit: AuditResult,
    *,
    exclude_fingerprints: set[str] | None = None,
) -> list[tuple[SourceRecord, CandidateScore, Reconciliation, str]]:
    excluded = exclude_fingerprints or set()
    items = [
        (item.scan.source, item.scores[0], item.reconciliation, item.local.evidence_hash())
        for item in audit.items
        if item.scores and item.scan.source.sha256 not in excluded
    ]
    return batch_eligible_items(items)


def _hydrate_for_acceptance(
    selected: CandidateScore,
    providers: tuple[Provider, ...],
    output: Console,
) -> tuple[CandidateScore, HydrationResult] | None:
    result = hydrate_candidate(selected.candidate, providers)
    if result.status == "unavailable" and typer.confirm(
        f"Exact metadata enrichment failed: {result.error}. Retry?", default=False
    ):
        result = hydrate_candidate(selected.candidate, providers)
    if result.status == "conflict":
        output.print("Exact provider detail conflicts with the selected identity:")
        for conflict in result.conflicts:
            output.print(f"  {conflict}")
        output.print("Not accepted; review or search again.")
        return None
    if result.status == "unavailable":
        output.print(f"Exact metadata enrichment remains unavailable: {result.error}")
        if not typer.confirm(
            "Accept this candidate with explicitly incomplete provider metadata?", default=False
        ):
            output.print("Not accepted; no decision was saved.")
            return None
        result = HydrationResult(
            selected.candidate,
            "sparse_explicit",
            error=result.error,
        )
    hydrated_score = replace(selected, candidate=result.candidate)
    if result.accepted_detail:
        _show_hydrated_metadata(output, result)
    return hydrated_score, result


def _show_hydrated_metadata(output: Console, result: HydrationResult) -> None:
    candidate = result.candidate
    output.print("Exact provider metadata:")
    if result.changes:
        output.print("  Enriched: " + ", ".join(result.changes))
    labels = {
        "writer": "Writer",
        "artist": "Artist",
        "penciller": "Penciller",
        "penciler": "Penciller",
        "inker": "Inker",
        "colorist": "Colorist",
        "letterer": "Letterer",
        "cover-artist": "Cover artist",
        "editor": "Editor",
        "translator": "Translator",
        "author": "Author",
    }
    grouped: dict[str, list[str]] = {}
    for contributor in candidate.creators:
        grouped.setdefault(labels.get(contributor.role, contributor.role), []).append(
            contributor.name
        )
    for role, names in grouped.items():
        output.print(f"  {role}: {', '.join(dict.fromkeys(names))}")


def _show_item(
    console: Console, item: ReviewItem, selected_rank: int | None, *, compact: bool = False
) -> None:
    console.rule(item.scan.source.path.name)
    console.print(
        f"Path: {item.scan.source.path}\n"
        f"Classification: {item.local.kind.value}/{item.local.subtype} "
        f"({item.local.classification_confidence:.2f})"
    )
    if item.local.kind.value == "comic":
        local_evidence = (
            f"Series: {item.local.series_title or item.local.title}\n"
            f"Issue: {item.local.sequence.normalized if item.local.sequence else 'unresolved'}"
        )
        if not compact:
            local_evidence += (
                f"\nLocal publication-year evidence: {item.local.year or 'none'}\n"
                f"Local run-start evidence: {item.local.run_start_year or 'none'}"
            )
        console.print(local_evidence)
        table = Table(
            "",
            "Rank",
            "Issue",
            "Run context",
            "Publication",
            "Writer",
            "Score",
            "Eligible",
        )
    else:
        console.print(f"Local title: {item.local.title}")
        table = Table(
            "",
            "Rank",
            "Candidate",
            "Author",
            "Publication",
            "Type",
            "Score",
            "Margin",
            "Eligible",
        )
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
                f"cover {score.candidate.cover_date or '-'}\n"
                f"release {score.candidate.release_date or '-'}\n"
                f"{score.candidate.publisher or '-'}",
                _candidate_writers(score.candidate),
                f"{score.score:.1f}",
                "yes" if score.eligible else "no",
            )
            continue
        table.add_row(
            marker,
            str(score.rank),
            score.candidate.title,
            _candidate_authors(score.candidate),
            _candidate_book_publication(score.candidate),
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


def _collection_search_text(local: LocalIdentity) -> str:
    series = (local.series_title or "").strip()
    title = local.title.strip()
    if series and title and title.casefold() != series.casefold():
        if title.casefold().startswith(series.casefold()):
            return title
        return f"{series} {title}"
    return title or series


def _candidate_authors(candidate: NormalizedCandidate) -> str:
    names = [item.name for item in candidate.creators if item.role == "author"]
    return ", ".join(dict.fromkeys(names)) or "-"


def _candidate_book_publication(candidate: NormalizedCandidate) -> str:
    return (
        f"{candidate.publication_date or '-'}\n"
        f"{candidate.publisher or '-'}"
    )


def _candidate_writers(candidate: NormalizedCandidate) -> str:
    names = [item.name for item in candidate.creators if item.role == "writer"]
    return ", ".join(dict.fromkeys(names)) or "-"


def _displayed_scores(item: ReviewItem) -> list[CandidateScore]:
    return usable_identity_scores(item.scores)[:9]


def _choose_comic_vine_run(
    item: ReviewItem,
    providers: tuple[Provider, ...],
    console: Console,
) -> NormalizedCandidate | None:
    series = item.local.series_title
    if item.local.kind.value != "comic" or not series:
        console.print("This item has no local comic series to resolve.")
        return None
    provider = next(
        (value for value in providers if isinstance(value, ComicVineProvider)),
        None,
    )
    if provider is None:
        console.print("Comic Vine is unavailable for run selection.")
        return None
    try:
        runs = provider.search_runs(
            SearchQuery(
                item.local.kind,
                series,
                series_title=series,
                item_type="run",
            )
        )
    except ProviderError as exc:
        console.print(f"Comic Vine run lookup failed: {exc}")
        return None

    key = run_group_key(series)
    matching = [
        run
        for run in runs
        if run.record_type is RecordType.COMIC_RUN
        and run.series_title
        and run_group_key(run.series_title) == key
        and run.run_start_year is not None
    ]
    if not matching:
        console.print(
            "No Comic Vine run with an explicit start year is available for this series."
        )
        return None

    table = Table(title=f"Selectable Comic Vine runs for {series}")
    table.add_column("Rank")
    table.add_column("Series")
    table.add_column("Start")
    table.add_column("Publisher")
    table.add_column("Run ID")
    for rank, run in enumerate(matching, start=1):
        table.add_row(
            str(rank),
            run.series_title or run.title,
            str(run.run_start_year),
            run.publisher or "-",
            run.provider_id,
        )
    console.print(table)
    rank = int(typer.prompt("Run rank", type=int))
    if not 1 <= rank <= len(matching):
        console.print(f"Run rank {rank} is not displayed.")
        return None
    return matching[rank - 1]


def _mark_incompatible_group_decisions(
    repository: DecisionRepository,
    audit: AuditResult,
    group_key: str,
    selected_run_id: str,
) -> int:
    changed = 0
    for item in audit.items:
        series = item.local.series_title
        if not series or run_group_key(series) != group_key:
            continue
        latest = repository.latest(item.scan.source)
        if latest is None or latest.decision_type not in {
            DecisionType.ACCEPTED,
            DecisionType.WORK_ACCEPTED,
        }:
            continue
        candidate = latest.payload.get("candidate")
        run_id = candidate.get("run_id") if isinstance(candidate, dict) else None
        if run_id == selected_run_id:
            continue
        repository.add(
            item.scan.source,
            DecisionType.UNRESOLVED,
            item.local.evidence_hash(),
            payload={
                "reason": "run_group_changed",
                "selected_run_id": selected_run_id,
                "explicit": True,
            },
        )
        changed += 1
    return changed


def _action_prompt(
    item: ReviewItem, audit: AuditResult, *, wizard_mode: bool = False
) -> str:
    displayed = _displayed_scores(item)
    if wizard_mode:
        if displayed:
            actions = ["[A] Accept", "[V] View why"]
            if len(displayed) > 1 or not displayed[0].eligible:
                actions.append("[C] Choose candidate")
            actions.extend(["[N] Next", "[M] More actions", "[Q] Save and quit"])
            return "  ".join(actions)
        return "  ".join(
            [
                "[S] Search",
                "[I] Enter identity",
                "[U] Unresolved",
                "[N] Next",
                "[M] More actions",
                "[Q] Save and quit",
            ]
        )
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


def _show_summary(
    console: Console,
    counts: dict[str, int],
    total: int,
    *,
    repository: DecisionRepository | None = None,
    audit: AuditResult | None = None,
) -> None:
    if repository is not None and audit is not None:
        counts, no_decision = _persisted_review_counts(repository, audit)
    else:
        no_decision = max(total - sum(counts.values()), 0)
    console.print("\nReview complete.\n")
    console.print(f"Accepted:    {counts['accepted']}")
    console.print(f"Work-only:   {counts['work_only']}")
    console.print(f"Manual:      {counts['manual']}")
    console.print(f"Rejected:    {counts['rejected']}")
    console.print(f"Unresolved:  {counts['unresolved']}")
    console.print(f"Skipped:     {counts['skipped']}")
    console.print(f"No decision: {no_decision}")
    console.print("\nNo media files were modified.")


def _persisted_review_counts(
    repository: DecisionRepository, audit: AuditResult
) -> tuple[dict[str, int], int]:
    counts = {
        key: 0
        for key in ("accepted", "work_only", "manual", "rejected", "unresolved", "skipped")
    }
    mapping = {
        DecisionType.ACCEPTED: "accepted",
        DecisionType.WORK_ACCEPTED: "work_only",
        DecisionType.MANUAL_IDENTITY: "manual",
        DecisionType.REJECTED: "rejected",
        DecisionType.UNRESOLVED: "unresolved",
        DecisionType.SKIPPED: "skipped",
    }
    no_decision = 0
    for item in audit.items:
        decision = repository.latest(item.scan.source)
        key = (
            mapping.get(decision.decision_type)
            if decision is not None
            and decision.source_evidence_hash == item.local.evidence_hash()
            else None
        )
        if key is None:
            no_decision += 1
        else:
            counts[key] += 1
    return counts, no_decision


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
    if item_type in {"issue", "annual", "special"}:
        fields["sequence"] = typer.prompt(
            "Sequence", default=local.sequence.raw if local.sequence else "1"
        )
    elif item_type in {"trade", "collected-edition", "omnibus"}:
        sequence = typer.prompt(
            "Sequence (optional)",
            default=local.sequence.raw if local.sequence else "",
            show_default=local.sequence is not None,
        ).strip()
        if sequence:
            fields["sequence"] = sequence
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
