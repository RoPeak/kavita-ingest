from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from .collection_candidates import collection_edition_qualifiers, normalize_collection_title
from .config import MatchingSettings
from .domain import Classification, MediaKind, SequenceNumber
from .providers.models import Identifier, NormalizedCandidate, RecordType


class ComparisonKind(StrEnum):
    EXACT = "exact"
    SIMILAR = "similar"
    SUPPORTING = "supporting"
    MISSING = "missing"
    CONFLICT = "conflict"
    HARD_CONTRADICTION = "hard_contradiction"


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    kind: MediaKind
    subtype: str
    classification_confidence: float
    title: str
    creators: tuple[str, ...] = ()
    identifiers: tuple[Identifier, ...] = ()
    series_title: str | None = None
    sequence: SequenceNumber | None = None
    year: int | None = None
    run_start_year: int | None = None
    publisher: str | None = None
    language: str | None = None
    edition_qualifiers: tuple[str, ...] = ()

    def evidence_hash(self) -> str:
        fields = asdict(self)
        # Keep historical decision evidence stable for sources that gain no new
        # edition evidence. Only identities with a real qualifier should become
        # stale under this parser upgrade.
        if not self.edition_qualifiers:
            fields.pop("edition_qualifiers", None)
        payload = json.dumps(fields, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FieldComparison:
    field: str
    local_value: str | None
    candidate_value: str | None
    kind: ComparisonKind
    score_delta: float
    confidence: float
    reason: str
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate: NormalizedCandidate
    score: float
    classification_confidence: float
    comparisons: tuple[FieldComparison, ...]
    contradictions: tuple[str, ...]
    hard_contradiction: bool
    identity_fields_high: bool
    rank: int = 0
    runner_up_margin: float = 0.0
    eligible: bool = False
    suppressed: bool = False

    def explanation(self) -> tuple[str, ...]:
        lines = [
            f"Candidate {self.candidate.key}: score {self.score:.1f}",
            f"Classification confidence: {self.classification_confidence:.2f}",
            f"Runner-up margin: {self.runner_up_margin:.1f}",
        ]
        lines.extend(
            f"{item.field}: {item.kind.value} ({item.score_delta:+.1f}) - {item.reason}"
            for item in self.comparisons
        )
        lines.extend(f"CONTRADICTION: {item}" for item in self.contradictions)
        lines.append(f"Eligible for explicit acceptance: {'yes' if self.eligible else 'no'}")
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class FieldResolution:
    field: str
    value: Any
    confidence: float
    provenance: tuple[str, ...]
    decision_source: str
    alternatives: tuple[Any, ...] = ()
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Reconciliation:
    work_state: str | None
    edition_state: str | None
    fields: tuple[FieldResolution, ...]
    reason: tuple[str, ...]


def local_identity(classification: Classification, metadata: dict[str, Any]) -> LocalIdentity:
    hypothesis = classification.hypotheses[0]
    identifiers = _local_identifiers(metadata)
    publisher = _string(metadata.get("publisher"))
    language = _string(metadata.get("language"))
    creators = hypothesis.creators
    if classification.kind is MediaKind.COMIC:
        comicinfo = metadata.get("comicinfo")
        if isinstance(comicinfo, dict):
            publisher = _string(comicinfo.get("Publisher")) or publisher
            language = _string(comicinfo.get("LanguageISO")) or language
            if not creators:
                creators = _split_names(_string(comicinfo.get("Writer")))
    return LocalIdentity(
        classification.kind,
        hypothesis.subtype,
        classification.confidence,
        (hypothesis.title or "")
        if classification.kind is MediaKind.COMIC
        else hypothesis.title or hypothesis.series or "",
        creators,
        identifiers,
        hypothesis.series,
        hypothesis.sequence,
        hypothesis.year,
        None,
        publisher,
        language,
        hypothesis.edition_qualifiers,
    )


def score_candidates(
    local: LocalIdentity,
    candidates: list[NormalizedCandidate],
    settings: MatchingSettings,
) -> list[CandidateScore]:
    raw = [_score(local, candidate) for candidate in candidates]
    raw.sort(key=lambda item: (-item.score, item.candidate.key))
    output: list[CandidateScore] = []
    for index, item in enumerate(raw):
        runner = raw[index + 1].score if index + 1 < len(raw) else 0.0
        margin = item.score - runner if index == 0 else 0.0
        eligible = (
            index == 0
            and item.score >= settings.eligible_score
            and margin >= settings.eligible_margin
            and local.classification_confidence >= settings.classification_confidence
            and not item.hard_contradiction
            and item.identity_fields_high
            and candidate_planning_context_ready(local, item.candidate)
        )
        output.append(
            CandidateScore(
                item.candidate,
                item.score,
                item.classification_confidence,
                item.comparisons,
                item.contradictions,
                item.hard_contradiction,
                item.identity_fields_high,
                index + 1,
                margin,
                eligible,
                item.suppressed,
            )
        )
    return output


def usable_identity_scores(scores: Iterable[CandidateScore]) -> list[CandidateScore]:
    """Return candidates suitable for normal identity-selection displays."""
    return [
        score
        for score in scores
        if score.score > 0
        and not score.hard_contradiction
        and score.candidate.record_type is not RecordType.COMIC_RUN
    ]


def reconcile(local: LocalIdentity, score: CandidateScore | None) -> Reconciliation:
    if score is None:
        return Reconciliation(
            "unresolved" if local.kind is MediaKind.BOOK else None,
            "unresolved" if local.kind is MediaKind.BOOK else None,
            (),
            ("no external candidate",),
        )
    candidate = score.candidate
    fields = []
    mapping = {
        "title": candidate.title,
        "publisher": candidate.publisher,
        "publication_date": candidate.publication_date,
        "release_date": candidate.release_date,
        "release_date_precision": candidate.release_date_precision,
        "cover_date": candidate.cover_date,
        "cover_date_precision": candidate.cover_date_precision,
        "language": candidate.language,
        "series_title": candidate.series_title,
        "sequence": candidate.sequence.normalized if candidate.sequence else None,
    }
    comparisons = {item.field: item for item in score.comparisons}
    for field, value in mapping.items():
        if value is None:
            continue
        comparison = comparisons.get(field)
        confidence = comparison.confidence if comparison else min(score.score / 100, 0.85)
        fields.append(
            FieldResolution(
                field,
                value,
                confidence,
                (candidate.provider.value, candidate.provider_id),
                "provider_candidate",
                conflicts=(comparison.reason,)
                if comparison and comparison.kind is ComparisonKind.CONFLICT
                else (),
            )
        )
    if local.kind is not MediaKind.BOOK:
        return Reconciliation(None, None, tuple(fields), tuple(score.contradictions))
    exact_edition_identifier = any(
        item.field == "identifier" and item.kind is ComparisonKind.EXACT
        for item in score.comparisons
    )
    work_state = "accepted" if score.score >= 80 and not score.hard_contradiction else "unresolved"
    edition_state = (
        "accepted"
        if candidate.record_type is RecordType.BOOK_EDITION
        and exact_edition_identifier
        and score.score >= 92
        else "unresolved"
    )
    reasons = []
    if work_state == "accepted" and edition_state == "unresolved":
        reasons.append(
            "work identity is strong but no exact edition identifier resolves the edition"
        )
    reasons.extend(score.contradictions)
    return Reconciliation(work_state, edition_state, tuple(fields), tuple(reasons))


def _score(local: LocalIdentity, candidate: NormalizedCandidate) -> CandidateScore:
    comparisons: list[FieldComparison] = []
    contradictions: list[str] = []
    if local.kind is not candidate.media_kind:
        contradictions.append("book/comic media type conflict")
        comparisons.append(
            _comparison(
                "media_kind",
                local.kind,
                candidate.media_kind,
                ComparisonKind.HARD_CONTRADICTION,
                -100,
                1,
                contradictions[-1],
            )
        )
    collected = local.subtype == "collected-edition"
    if local.kind is MediaKind.COMIC and candidate.record_type is RecordType.COMIC_RUN:
        contradictions.append("comic run records provide context and cannot identify a media item")
        comparisons.append(
            _comparison(
                "item_type",
                local.subtype,
                candidate.record_type,
                ComparisonKind.HARD_CONTRADICTION,
                -100,
                1,
                contradictions[-1],
            )
        )
    if collected and candidate.record_type is RecordType.COMIC_ISSUE:
        contradictions.append("collected edition cannot resolve to a regular issue")
        comparisons.append(
            _comparison(
                "item_type",
                local.subtype,
                candidate.record_type,
                ComparisonKind.HARD_CONTRADICTION,
                -100,
                1,
                contradictions[-1],
            )
        )
    if (
        not collected
        and local.kind is MediaKind.COMIC
        and candidate.record_type is RecordType.COMIC_COLLECTION
    ):
        contradictions.append("regular issue evidence conflicts with a collected edition candidate")
        comparisons.append(
            _comparison(
                "item_type",
                local.subtype,
                candidate.record_type,
                ComparisonKind.HARD_CONTRADICTION,
                -100,
                1,
                contradictions[-1],
            )
        )

    _identifier_score(local, candidate, comparisons, contradictions)
    local_title: str | None
    candidate_title: str | None
    if collected:
        local_title, similarity = _collection_title_similarity(local, candidate.title)
        candidate_title = candidate.title
    else:
        local_title = local.series_title if local.kind is MediaKind.COMIC else local.title
        candidate_title = (
            candidate.series_title if local.kind is MediaKind.COMIC else candidate.title
        )
        similarity = _similarity(local_title or "", candidate_title or "")
    if similarity >= 0.96:
        comparisons.append(
            _comparison(
                "title",
                local_title,
                candidate_title,
                ComparisonKind.EXACT,
                32,
                similarity,
                "normalized titles agree",
            )
        )
    elif similarity >= 0.82:
        comparisons.append(
            _comparison(
                "title",
                local_title,
                candidate_title,
                ComparisonKind.SIMILAR,
                22,
                similarity,
                "titles are strongly similar",
            )
        )
    elif similarity >= 0.65:
        comparisons.append(
            _comparison(
                "title",
                local_title,
                candidate_title,
                ComparisonKind.SUPPORTING,
                8,
                similarity,
                "titles partially agree",
            )
        )
    else:
        comparisons.append(
            _comparison(
                "title",
                local_title,
                candidate_title,
                ComparisonKind.CONFLICT,
                -18,
                1 - similarity,
                "titles disagree",
            )
        )

    if (
        not collected
        and local.kind is MediaKind.COMIC
        and local.title
        and candidate.title
        and _normalize(local.title) != _normalize(local.series_title or "")
    ):
        issue_similarity = _similarity(local.title, candidate.title)
        if issue_similarity >= 0.9:
            comparisons.append(
                _comparison(
                    "issue_title",
                    local.title,
                    candidate.title,
                    ComparisonKind.EXACT,
                    12,
                    issue_similarity,
                    "issue titles agree",
                )
            )
        elif issue_similarity >= 0.7:
            comparisons.append(
                _comparison(
                    "issue_title",
                    local.title,
                    candidate.title,
                    ComparisonKind.SUPPORTING,
                    6,
                    issue_similarity,
                    "issue titles are similar",
                )
            )
        elif issue_similarity < 0.35:
            comparisons.append(
                _comparison(
                    "issue_title",
                    local.title,
                    candidate.title,
                    ComparisonKind.CONFLICT,
                    -8,
                    1 - issue_similarity,
                    "issue titles disagree",
                )
            )

    local_creators = {_normalize(name) for name in local.creators}
    candidate_creators = {_normalize(item.name) for item in candidate.creators}
    if local_creators and candidate_creators:
        overlap = local_creators & candidate_creators
        comparisons.append(
            _comparison(
                "creators",
                ", ".join(local.creators),
                ", ".join(item.name for item in candidate.creators),
                ComparisonKind.EXACT if overlap else ComparisonKind.CONFLICT,
                18 if overlap else -12,
                1.0,
                "creator names overlap" if overlap else "creator names disagree",
            )
        )

    if collected and local.publisher and candidate.publisher:
        publisher_similarity = _publisher_similarity(local.publisher, candidate.publisher)
        if publisher_similarity >= 0.82:
            comparisons.append(
                _comparison(
                    "publisher",
                    local.publisher,
                    candidate.publisher,
                    ComparisonKind.SUPPORTING,
                    8,
                    publisher_similarity,
                    "collection publishers agree",
                )
            )
        elif publisher_similarity < 0.45:
            comparisons.append(
                _comparison(
                    "publisher",
                    local.publisher,
                    candidate.publisher,
                    ComparisonKind.CONFLICT,
                    -20,
                    1 - publisher_similarity,
                    "collection publishers disagree",
                )
            )

    if collected and local.edition_qualifiers:
        candidate_qualifiers = collection_edition_qualifiers(
            candidate.title, candidate.subtitle
        )
        for qualifier in local.edition_qualifiers:
            best, similarity = _best_qualifier_match(qualifier, candidate_qualifiers)
            if best is not None and similarity >= 0.90:
                comparisons.append(
                    _comparison(
                        "edition_qualifier",
                        qualifier,
                        best,
                        ComparisonKind.EXACT,
                        18,
                        similarity,
                        "edition qualifier agrees",
                    )
                )
            elif best is not None and similarity >= 0.72:
                comparisons.append(
                    _comparison(
                        "edition_qualifier",
                        qualifier,
                        best,
                        ComparisonKind.SUPPORTING,
                        10,
                        similarity,
                        "edition qualifier is strongly similar",
                    )
                )
            elif candidate_qualifiers:
                comparisons.append(
                    _comparison(
                        "edition_qualifier",
                        qualifier,
                        ", ".join(candidate_qualifiers),
                        ComparisonKind.CONFLICT,
                        -30,
                        1 - similarity,
                        "edition qualifier conflicts with provider edition family",
                    )
                )
            else:
                comparisons.append(
                    _comparison(
                        "edition_qualifier",
                        qualifier,
                        None,
                        ComparisonKind.MISSING,
                        -20,
                        1,
                        "distinctive local edition qualifier is absent from provider title",
                    )
                )

    if local.sequence and candidate.sequence:
        exact = local.sequence.normalized == candidate.sequence.normalized
        collection_sequence_source = (
            candidate.provider_metadata.get("collection_sequence_source") if collected else None
        )
        contextual_collection_sequence = collection_sequence_source in {
            "local",
            "provider_title",
        }
        kind = ComparisonKind.EXACT if exact else ComparisonKind.HARD_CONTRADICTION
        delta = 0 if contextual_collection_sequence and exact else (30 if exact else -100)
        reason = (
            "collection sequence agrees with explicit filename/provider-title evidence"
            if contextual_collection_sequence and exact
            else ("sequence numbers agree" if exact else "sequence numbers conflict")
        )
        comparisons.append(
            _comparison(
                "sequence",
                local.sequence.normalized,
                candidate.sequence.normalized,
                kind,
                delta,
                1,
                reason,
            )
        )
        if not exact and local.kind is MediaKind.COMIC:
            contradictions.append(reason)

    candidate_year = _candidate_year(candidate, local)
    if local.year and candidate_year:
        difference = abs(local.year - candidate_year)
        if difference == 0:
            comparisons.append(
                _comparison(
                    "year", local.year, candidate_year, ComparisonKind.EXACT, 8, 1, "years agree"
                )
            )
        elif difference == 1:
            comparisons.append(
                _comparison(
                    "year",
                    local.year,
                    candidate_year,
                    ComparisonKind.SUPPORTING,
                    3,
                    0.6,
                    "years differ by one",
                )
            )
        else:
            comparisons.append(
                _comparison(
                    "year",
                    local.year,
                    candidate_year,
                    ComparisonKind.CONFLICT,
                    -7,
                    min(difference / 10, 1),
                    "years disagree",
                )
            )
    if local.run_start_year and candidate.run_start_year:
        exact_run = local.run_start_year == candidate.run_start_year
        comparisons.append(
            _comparison(
                "run_start_year",
                local.run_start_year,
                candidate.run_start_year,
                ComparisonKind.EXACT if exact_run else ComparisonKind.CONFLICT,
                8 if exact_run else -10,
                1,
                "run-start years agree" if exact_run else "run-start years disagree",
            )
        )
    total = max(0.0, min(100.0, 40.0 + sum(item.score_delta for item in comparisons)))
    hard = any(item.kind is ComparisonKind.HARD_CONTRADICTION for item in comparisons)
    title_high = any(item.field == "title" and item.confidence >= 0.82 for item in comparisons)
    sequence_high = (
        local.kind is not MediaKind.COMIC
        or local.sequence is None
        or any(
            item.field == "sequence" and item.kind is ComparisonKind.EXACT for item in comparisons
        )
    )
    publisher_high = not any(
        item.field == "publisher" and item.kind is ComparisonKind.CONFLICT
        for item in comparisons
    )
    qualifier_high = (
        not local.edition_qualifiers
        or all(
            any(
                item.field == "edition_qualifier"
                and item.local_value == qualifier
                and item.kind in {ComparisonKind.EXACT, ComparisonKind.SUPPORTING}
                for item in comparisons
            )
            for qualifier in local.edition_qualifiers
        )
    )
    return CandidateScore(
        candidate,
        total,
        local.classification_confidence,
        tuple(comparisons),
        tuple(contradictions),
        hard,
        title_high and sequence_high and publisher_high and qualifier_high,
    )


def _identifier_score(
    local: LocalIdentity,
    candidate: NormalizedCandidate,
    comparisons: list[FieldComparison],
    contradictions: list[str],
) -> None:
    local_by_scheme = _identifier_map(local.identifiers)
    candidate_by_scheme = _identifier_map(candidate.identifiers)
    for scheme in sorted(local_by_scheme.keys() & candidate_by_scheme.keys()):
        overlap = local_by_scheme[scheme] & candidate_by_scheme[scheme]
        if overlap:
            comparisons.append(
                _comparison(
                    "identifier",
                    ",".join(local_by_scheme[scheme]),
                    ",".join(candidate_by_scheme[scheme]),
                    ComparisonKind.EXACT,
                    55,
                    1,
                    f"exact {scheme} identifier",
                )
            )
        else:
            reason = f"conflicting exact {scheme} identifiers"
            comparisons.append(
                _comparison(
                    "identifier",
                    ",".join(local_by_scheme[scheme]),
                    ",".join(candidate_by_scheme[scheme]),
                    ComparisonKind.HARD_CONTRADICTION,
                    -100,
                    1,
                    reason,
                )
            )
            contradictions.append(reason)


def _local_identifiers(metadata: dict[str, Any]) -> tuple[Identifier, ...]:
    output = []
    for value in metadata.get("identifiers", []):
        text = str(value).strip()
        digits = re.sub(r"[^0-9Xx]", "", text)
        if len(digits) in {10, 13}:
            output.append(Identifier("isbn", digits.upper()))
    comicinfo = metadata.get("comicinfo", {})
    if isinstance(comicinfo, dict) and comicinfo.get("GTIN"):
        output.append(Identifier("gtin", str(comicinfo["GTIN"])))
    return tuple(output)


def _identifier_map(values: tuple[Identifier, ...]) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    for item in values:
        output.setdefault(item.scheme.casefold(), set()).add(_normalize(item.value))
    return output


def _comparison(
    field: str,
    local: object,
    candidate: object,
    kind: ComparisonKind,
    delta: float,
    confidence: float,
    reason: str,
) -> FieldComparison:
    return FieldComparison(
        field,
        str(local) if local is not None else None,
        str(candidate) if candidate is not None else None,
        kind,
        delta,
        confidence,
        reason,
        ("local", "provider"),
    )


def _best_qualifier_match(
    local_qualifier: str,
    candidate_qualifiers: tuple[str, ...],
) -> tuple[str | None, float]:
    if not candidate_qualifiers:
        return None, 0.0
    scored = [
        (candidate, _qualifier_similarity(local_qualifier, candidate))
        for candidate in candidate_qualifiers
    ]
    return max(scored, key=lambda item: item[1])


def _qualifier_similarity(left: str, right: str) -> float:
    left_key = _normalize(left)
    right_key = _normalize(right)
    if left_key == right_key:
        return 1.0
    if left_key and right_key and (left_key in right_key or right_key in left_key):
        shorter = min(len(left_key), len(right_key))
        longer = max(len(left_key), len(right_key))
        return shorter / longer
    return SequenceMatcher(None, left_key, right_key).ratio()


def _collection_title_similarity(
    local: LocalIdentity, candidate_title: str
) -> tuple[str, float]:
    values = [local.title]
    if local.series_title:
        values.append(f"{local.series_title} {local.title}".strip())
        if local.creators:
            values.append(
                f"{local.series_title} by {local.creators[0]} {local.title}".strip()
            )
    best = max(values, key=lambda value: _collection_similarity(value, candidate_title))
    return best, _collection_similarity(best, candidate_title)


def _collection_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        normalize_collection_title(left),
        normalize_collection_title(right),
    ).ratio()


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _publisher_similarity(left: str, right: str) -> float:
    left_key = _publisher_key(left)
    right_key = _publisher_key(right)
    if left_key and right_key and left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def _publisher_key(value: str) -> str:
    generic = {
        "book",
        "books",
        "comic",
        "comics",
        "entertainment",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "publisher",
        "publishers",
        "publishing",
    }
    tokens = [token for token in _normalize(value).split() if token not in generic]
    return " ".join(tokens)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _candidate_year(
    candidate: NormalizedCandidate,
    local: LocalIdentity | None = None,
) -> int | None:
    if (
        candidate.media_kind is MediaKind.BOOK
        and local is not None
        and local.year is not None
    ):
        title_year = re.search(
            r"\b((?:19|20)\d{2})\s*$",
            candidate.title,
        )
        if title_year:
            title_without_year = candidate.title[: title_year.start()].rstrip(
                " -_:"
            )
            if _similarity(local.title, title_without_year) >= 0.82:
                return int(title_year.group(1))

    value = (
        candidate.publication_date
        if local is not None and local.subtype == "collected-edition"
        else (
            candidate.cover_date
            if candidate.media_kind is MediaKind.COMIC
            else candidate.publication_date
        )
    )
    match = re.match(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


def candidate_planning_context_ready(
    local: LocalIdentity,
    candidate: NormalizedCandidate,
) -> bool:
    """Return whether a provider candidate can satisfy mandatory plan identity fields.

    Normal comic issues/annuals/specials require a resolved run-start year in the
    canonical identity. A high matching score must never advertise such a
    candidate as plan-eligible when the planner is guaranteed to reject it.
    """
    if (
        local.kind is MediaKind.COMIC
        and local.subtype in {"issue", "annual", "special"}
    ):
        return candidate.run_start_year is not None
    return True


def _split_names(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        part.strip()
        for part in re.split(r"\s*(?:,|;|\band\b|&)\s*", value)
        if part.strip()
    )


def _string(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int)) else None
