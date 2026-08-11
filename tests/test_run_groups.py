from __future__ import annotations

from pathlib import Path

from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind
from kavita_ingest.matching import CandidateScore
from kavita_ingest.providers.models import NormalizedCandidate, ProviderName, RecordType
from kavita_ingest.run_groups import RunGroupRepository, constrain_to_selected_run


def _score(run_id: str) -> CandidateScore:
    candidate = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        f"issue-{run_id}",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Issue title",
        series_title="Watchmen",
        run_id=run_id,
    )
    return CandidateScore(candidate, 95.0, 0.98, (), (), False, True, eligible=True)


def test_run_group_choice_is_append_only_clearable_and_never_accepts_issue(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    with connect(database) as connection:
        repository = RunGroupRepository(connection)
        selected = repository.choose(
            "comic:watchmen", "comic_vine", "4050-123", {"name": "Watchmen", "year": 1986}
        )
        scores = (_score("4050-999"), _score("4050-123"))
        assert constrain_to_selected_run(scores, selected) == (scores[1],)
        assert connection.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0

        overridden = repository.choose(
            "comic:watchmen",
            "comic_vine",
            "4050-456",
            {"name": "Watchmen", "year": 2024},
            manual=True,
        )
        cleared = repository.clear("comic:watchmen", "comic_vine")
        assert overridden.supersedes_id == selected.id
        assert cleared.supersedes_id == overridden.id
        assert constrain_to_selected_run(scores, cleared) == scores
        assert [
            item.decision_type.value for item in repository.history("comic:watchmen", "comic_vine")
        ] == ["selected", "manual", "cleared"]
