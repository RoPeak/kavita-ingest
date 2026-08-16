from __future__ import annotations

from pathlib import Path

from kavita_ingest.config import MatchingSettings
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import DecisionRepository, accept_candidate
from kavita_ingest.domain import (
    InspectionResult,
    InspectionStatus,
    MediaKind,
    SourceFormat,
    SourceRecord,
)
from kavita_ingest.matching import local_identity, reconcile, score_candidates
from kavita_ingest.parsing import classify
from kavita_ingest.projection import project_comic
from kavita_ingest.providers.models import (
    Contributor,
    NormalizedCandidate,
    ProviderName,
    RecordType,
)
from kavita_ingest.resolution import resolve_explicit_identity


def test_corrected_saga_publication_round_trips_as_collection_volume(tmp_path: Path) -> None:
    inspection = InspectionResult(
        InspectionStatus.OK,
        SourceFormat.CBZ,
        metadata={
            "comicinfo": {
                "Series": "Saga",
                "Title": "Saga Volume 2",
                "Number": "2",
                "Writer": "Brian K. Vaughan, Fiona Staples",
                "Publisher": "Image Comics",
            }
        },
    )
    classification = classify(Path("Saga, Vol. 2 (2013).cbz"), SourceFormat.CBZ, inspection)
    local = local_identity(classification, inspection.metadata)
    candidate = NormalizedCandidate(
        ProviderName.OPEN_LIBRARY,
        "saga-vol-2-2013",
        RecordType.COMIC_COLLECTION,
        MediaKind.COMIC,
        "Saga, Vol. 2",
        creators=(
            Contributor("Brian K. Vaughan", "writer"),
            Contributor("Fiona Staples", "artist"),
        ),
        publisher="Image Comics",
        publication_date="2013-06-19",
        series_title="Saga",
        sequence=local.sequence,
        item_type="collected-edition",
    )
    score = score_candidates(local, [candidate], MatchingSettings())[0]
    assert score.eligible

    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = SourceRecord(
        tmp_path / "Saga, Vol. 2 (2013).cbz",
        10,
        1,
        "a" * 64,
        SourceFormat.CBZ,
        "zip",
    )
    with connect(database) as connection:
        repository = DecisionRepository(connection)
        accept_candidate(
            repository,
            source,
            score,
            reconcile(local, score),
            local.evidence_hash(),
            local_identity=local,
        )
        resolved = resolve_explicit_identity(repository, source, MediaKind.COMIC)

    assert resolved.identity is not None
    assert resolved.identity.collection_volume == 2
    projection = project_comic(resolved.identity)
    assert projection.metadata["Series"] == "Saga"
    assert projection.metadata["Number"] == ""
    assert projection.metadata["Volume"] == 2
    assert projection.destination_folder.as_posix() == "Saga/Specials"
    assert projection.filename.startswith("Saga - v02 - Saga, Vol. 2")
