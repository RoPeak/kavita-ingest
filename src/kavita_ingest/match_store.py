from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime

from .matching import CandidateScore, LocalIdentity


class MatchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def start_run(self, mode: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO match_runs(started_at, mode) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), mode),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("match run insert did not return an identifier")
        return int(cursor.lastrowid)

    def add_scores(
        self,
        run_id: int,
        source_id: int,
        local: LocalIdentity,
        scores: list[CandidateScore],
    ) -> None:
        for score in scores:
            self.connection.execute(
                """
                INSERT INTO match_candidates(run_id, source_id, rank, candidate_key,
                  candidate_json, score_json, source_evidence_hash, candidate_data_hash,
                  eligible, suppressed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_id,
                    score.rank,
                    score.candidate.key,
                    json.dumps(score.candidate.to_dict(), sort_keys=True, default=str),
                    json.dumps(asdict(score), sort_keys=True, default=str),
                    local.evidence_hash(),
                    score.candidate.data_hash(),
                    int(score.eligible),
                    int(score.suppressed),
                ),
            )
        self.connection.commit()

    def complete_run(self, run_id: int, source_count: int, summary: dict[str, int]) -> None:
        self.connection.execute(
            "UPDATE match_runs SET completed_at=?, source_count=?, summary_json=? WHERE id=?",
            (
                datetime.now(UTC).isoformat(),
                source_count,
                json.dumps(summary, sort_keys=True),
                run_id,
            ),
        )
        self.connection.commit()
