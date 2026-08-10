from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .candidates import CandidateGeneration, generate_candidates
from .config import AppConfig
from .db import connect, migrate
from .decisions import DecisionRepository
from .match_store import MatchRepository
from .matching import (
    CandidateScore,
    LocalIdentity,
    Reconciliation,
    local_identity,
    reconcile,
    score_candidates,
)
from .provider_runtime import build_providers
from .providers.base import Provider
from .scanner import ScanResult, scan


@dataclass(frozen=True, slots=True)
class ReviewItem:
    scan: ScanResult
    local: LocalIdentity
    generation: CandidateGeneration
    scores: tuple[CandidateScore, ...]
    reconciliation: Reconciliation


@dataclass(frozen=True, slots=True)
class AuditResult:
    items: tuple[ReviewItem, ...]
    summary: dict[str, int]
    run_id: int


def run_audit(
    root: Path,
    config: AppConfig,
    *,
    mode: str = "audit",
    providers_override: tuple[Provider, ...] | None = None,
) -> AuditResult:
    if config.database_path is None:
        raise ValueError("audit requires a database path for cache and match evidence")
    scans = scan(root, config, persist=True)
    migrate(config.database_path)
    connection = connect(config.database_path)
    try:
        providers = providers_override or build_providers(connection, config.providers)
        matches = MatchRepository(connection)
        decisions = DecisionRepository(connection)
        run_id = matches.start_run(mode)
        items = []
        for scanned in scans:
            local = local_identity(scanned.classification, scanned.inspection.metadata)
            generated = generate_candidates(local, providers)
            scores = score_candidates(local, list(generated.candidates), config.matching)
            scores = [
                replace(
                    score,
                    suppressed=decisions.rejection_suppresses(
                        scanned.source,
                        score.candidate.key,
                        local.evidence_hash(),
                        score.candidate.data_hash(),
                    ),
                    eligible=score.eligible
                    and not decisions.rejection_suppresses(
                        scanned.source,
                        score.candidate.key,
                        local.evidence_hash(),
                        score.candidate.data_hash(),
                    ),
                )
                for score in scores
            ]
            resolved = reconcile(local, scores[0] if scores else None)
            source_id = _source_id(connection, scanned)
            matches.add_scores(run_id, source_id, local, scores)
            items.append(ReviewItem(scanned, local, generated, tuple(scores), resolved))
        summary = _summary(items)
        matches.complete_run(run_id, len(items), summary)
        return AuditResult(tuple(items), summary, run_id)
    finally:
        connection.close()


def _source_id(connection: sqlite3.Connection, scanned: ScanResult) -> int:
    row = connection.execute(
        "SELECT id FROM sources WHERE path=?", (str(scanned.source.path),)
    ).fetchone()
    if row is None:
        raise RuntimeError("persisted source was not found")
    return int(row[0])


def _summary(items: list[ReviewItem]) -> dict[str, int]:
    summary = {
        "sources": len(items),
        "candidate_found": 0,
        "no_candidate": 0,
        "eligible_high_confidence": 0,
        "review_required": 0,
        "unresolved": 0,
        "provider_unavailable": 0,
        "work_accepted_edition_unresolved": 0,
        "hard_contradictions": 0,
        "collected_editions": 0,
        "collected_as_issue_contradictions": 0,
    }
    for item in items:
        if item.scores:
            summary["candidate_found"] += 1
            top = item.scores[0]
            if top.eligible:
                summary["eligible_high_confidence"] += 1
            else:
                summary["review_required"] += 1
            if top.hard_contradiction:
                summary["hard_contradictions"] += 1
        else:
            summary["no_candidate"] += 1
            summary["unresolved"] += 1
        if item.generation.unavailable:
            summary["provider_unavailable"] += 1
        if (
            item.reconciliation.work_state == "accepted"
            and item.reconciliation.edition_state == "unresolved"
        ):
            summary["work_accepted_edition_unresolved"] += 1
        if item.local.subtype == "collected-edition":
            summary["collected_editions"] += 1
            if item.scores and any(
                "collected edition cannot" in value for value in item.scores[0].contradictions
            ):
                summary["collected_as_issue_contradictions"] += 1
    return summary
