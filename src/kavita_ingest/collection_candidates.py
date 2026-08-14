from __future__ import annotations

from dataclasses import replace

from .domain import MediaKind, SequenceNumber
from .providers.models import Contributor, NormalizedCandidate, RecordType

_COLLECTION_ADAPTER = "book_edition"


def adapt_collection_candidate(
    candidate: NormalizedCandidate,
    *,
    series_title: str | None,
    sequence: SequenceNumber | None,
    item_type: str = "collected-edition",
) -> NormalizedCandidate | None:
    """Normalize a true edition record into a comic-collection identity candidate.

    Comic Vine's public volume schema does not identify TPB/hardcover/omnibus
    format, so ordinary Comic Vine volumes are intentionally not promoted into
    collection identities. Edition-capable book providers can identify the
    physical/digital collected edition; local parsing supplies only the series
    grouping and collection sequence that were already explicit in the source.
    """
    if candidate.record_type is RecordType.COMIC_COLLECTION:
        return replace(
            candidate,
            series_title=series_title or candidate.series_title,
            sequence=candidate.sequence or sequence,
            item_type=candidate.item_type or item_type,
            provider_metadata={
                **candidate.provider_metadata,
                "collection_series_source": "local" if series_title else "provider",
                **(
                    {"collection_sequence_source": "local"}
                    if candidate.sequence is None and sequence is not None
                    else {}
                ),
            },
        )
    if candidate.record_type is not RecordType.BOOK_EDITION:
        return None

    creators = tuple(
        Contributor(
            contributor.name,
            "writer" if contributor.role.casefold() == "author" else contributor.role,
        )
        for contributor in candidate.creators
    )
    return replace(
        candidate,
        record_type=RecordType.COMIC_COLLECTION,
        media_kind=MediaKind.COMIC,
        creators=creators,
        series_title=series_title,
        sequence=sequence,
        run_start_year=None,
        item_type=item_type,
        run_id=None,
        provider_metadata={
            **candidate.provider_metadata,
            "collection_adapter": _COLLECTION_ADAPTER,
            "collection_source_record_type": RecordType.BOOK_EDITION.value,
            "collection_series_source": "local",
            **({"collection_sequence_source": "local"} if sequence is not None else {}),
        },
    )


def adapt_exact_collection_candidate(
    selected: NormalizedCandidate,
    exact: NormalizedCandidate,
) -> NormalizedCandidate:
    """Re-apply collection semantics to an exact edition fetched for hydration."""
    if selected.provider_metadata.get("collection_adapter") != _COLLECTION_ADAPTER:
        return exact
    adapted = adapt_collection_candidate(
        exact,
        series_title=selected.series_title,
        sequence=selected.sequence,
        item_type=selected.item_type or "collected-edition",
    )
    return adapted if adapted is not None else exact
