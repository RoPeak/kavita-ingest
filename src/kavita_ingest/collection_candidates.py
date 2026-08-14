from __future__ import annotations

import re
from dataclasses import replace

from .domain import MediaKind, SequenceNumber
from .providers.models import Contributor, NormalizedCandidate, RecordType

_COLLECTION_ADAPTER = "book_edition"
_COLLECTION_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}
_COLLECTION_ROMAN_NUMERALS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
    "xi": "11",
    "xii": "12",
    "xiii": "13",
    "xiv": "14",
    "xv": "15",
    "xvi": "16",
    "xvii": "17",
    "xviii": "18",
    "xix": "19",
    "xx": "20",
}
_COLLECTION_SEQUENCE_RE = re.compile(
    r"\b(?P<label>book|volume|vol\.?)\s*[-:#]?\s*"
    r"(?P<number>\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|xx|xix|xviii|xvii|xvi|xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)\b",
    re.IGNORECASE,
)


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
    physical/digital collected edition; local parsing supplies the series
    grouping. When the local filename carries a collection number, the provider
    title must independently agree with that number rather than inheriting it
    silently from local evidence.
    """
    if _looks_like_single_issue(candidate.title):
        return None

    resolved_sequence = _resolve_collection_sequence(candidate, sequence)
    if sequence is not None and resolved_sequence is None:
        return None

    if candidate.record_type is RecordType.COMIC_COLLECTION:
        sequence_source = _collection_sequence_source(candidate, sequence)
        return replace(
            candidate,
            series_title=series_title or candidate.series_title,
            sequence=resolved_sequence,
            item_type=candidate.item_type or item_type,
            provider_metadata={
                **candidate.provider_metadata,
                "collection_series_source": "local" if series_title else "provider",
                **(
                    {"collection_sequence_source": sequence_source}
                    if sequence_source is not None
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
    sequence_source = _collection_sequence_source(candidate, sequence)
    return replace(
        candidate,
        record_type=RecordType.COMIC_COLLECTION,
        media_kind=MediaKind.COMIC,
        creators=creators,
        series_title=series_title,
        sequence=resolved_sequence,
        run_start_year=None,
        item_type=item_type,
        run_id=None,
        provider_metadata={
            **candidate.provider_metadata,
            "collection_adapter": _COLLECTION_ADAPTER,
            "collection_source_record_type": RecordType.BOOK_EDITION.value,
            "collection_series_source": "local",
            **(
                {"collection_sequence_source": sequence_source}
                if sequence_source is not None
                else {}
            ),
        },
    )


def collection_sequence_hint(
    title: str,
    subtitle: str | None = None,
) -> SequenceNumber | None:
    """Return an explicit Book/Volume number stated by provider title metadata."""
    for value in (title, subtitle or ""):
        match = _COLLECTION_SEQUENCE_RE.search(value)
        if match is None:
            continue
        number = _normalized_collection_number(match.group("number"))
        if number is not None:
            return SequenceNumber.parse(number)
    return None


def normalize_collection_title(value: str) -> str:
    """Normalize collection-number spelling without changing ordinary title words."""

    def replace_sequence(match: re.Match[str]) -> str:
        label = match.group("label").casefold()
        label = "volume" if label.startswith("vol") else "book"
        number = _normalized_collection_number(match.group("number"))
        return f"{label} {number}" if number is not None else match.group(0)

    normalized = _COLLECTION_SEQUENCE_RE.sub(replace_sequence, value.casefold())
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def collection_number_word(number: SequenceNumber | None) -> str | None:
    """Return a conservative English word alias for integer collection numbers 1..20."""
    if number is None or not number.normalized.isdigit():
        return None
    integer = int(number.normalized)
    if not 1 <= integer <= 20:
        return None
    return next(word for word, value in _COLLECTION_NUMBER_WORDS.items() if int(value) == integer)


def _resolve_collection_sequence(
    candidate: NormalizedCandidate,
    local_sequence: SequenceNumber | None,
) -> SequenceNumber | None:
    if local_sequence is None:
        return candidate.sequence
    external = candidate.sequence or collection_sequence_hint(candidate.title, candidate.subtitle)
    if external is None:
        return None
    if external.normalized != local_sequence.normalized:
        return None
    return local_sequence


def _collection_sequence_source(
    candidate: NormalizedCandidate,
    local_sequence: SequenceNumber | None,
) -> str | None:
    if local_sequence is None:
        return "provider" if candidate.sequence is not None else None
    if candidate.sequence is not None:
        return "provider"
    if collection_sequence_hint(candidate.title, candidate.subtitle) is not None:
        return "provider_title"
    return None


def _normalized_collection_number(value: str) -> str | None:
    normalized = value.casefold().rstrip(".")
    if normalized in _COLLECTION_NUMBER_WORDS:
        return _COLLECTION_NUMBER_WORDS[normalized]
    if normalized in _COLLECTION_ROMAN_NUMERALS:
        return _COLLECTION_ROMAN_NUMERALS[normalized]
    if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return SequenceNumber.parse(normalized).normalized
    return None


def _looks_like_single_issue(title: str) -> bool:
    """Reject book-catalog entries that are visibly individual comic issues."""
    return bool(re.search(r"(?:^|\s)(?:#|issue\s+)\d+(?:\.\d+)?\b", title, re.IGNORECASE))


def adapt_exact_collection_candidate(
    selected: NormalizedCandidate,
    exact: NormalizedCandidate,
) -> NormalizedCandidate:
    """Re-apply collection semantics to an exact edition fetched for hydration.

    Search responses can carry edition-disambiguating title/subtitle evidence that
    an exact provider response omits. The selected candidate has already crossed
    the collection safety boundary using provider-derived evidence, so a sparse
    exact response for the same provider identity must not erase that semantic
    adaptation. An explicit contradictory collection number is still preserved so
    hydration can reject it as an identity conflict.
    """
    if selected.provider_metadata.get("collection_adapter") != _COLLECTION_ADAPTER:
        return exact
    if exact.record_type is not RecordType.BOOK_EDITION:
        return exact

    exact_sequence = exact.sequence or collection_sequence_hint(exact.title, exact.subtitle)
    if (
        selected.sequence is not None
        and exact_sequence is not None
        and exact_sequence.normalized != selected.sequence.normalized
    ):
        contradictory = adapt_collection_candidate(
            exact,
            series_title=selected.series_title,
            sequence=exact_sequence,
            item_type=selected.item_type or "collected-edition",
        )
        return contradictory if contradictory is not None else exact

    adapted = adapt_collection_candidate(
        exact,
        series_title=selected.series_title,
        sequence=selected.sequence if exact_sequence is not None else None,
        item_type=selected.item_type or "collected-edition",
    )
    return adapted if adapted is not None else exact
