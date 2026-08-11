from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .canonical import CanonicalIdentity, ResolutionLevel
from .decisions import DecisionRecord, DecisionRepository, DecisionType
from .domain import MediaKind, SequenceNumber, SourceRecord
from .providers.models import NormalizedCandidate, RecordType


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    identity: CanonicalIdentity | None
    authorization: DecisionRecord | None
    blocks: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.identity is not None and not self.blocks


def resolve_explicit_identity(
    repository: DecisionRepository,
    source: SourceRecord,
    classified_kind: MediaKind,
) -> IdentityResolution:
    history = repository.history(source)
    if not history:
        return IdentityResolution(None, None, ("no explicit identity decision exists",))
    latest = history[-1]
    if latest.decision_type in {
        DecisionType.REJECTED,
        DecisionType.UNRESOLVED,
        DecisionType.SKIPPED,
    }:
        return IdentityResolution(
            None, latest, (f"latest explicit decision is {latest.decision_type.value}",)
        )
    authorization = next(
        (
            item
            for item in reversed(history)
            if item.decision_type
            in {DecisionType.ACCEPTED, DecisionType.WORK_ACCEPTED, DecisionType.MANUAL_IDENTITY}
        ),
        None,
    )
    if authorization is None:
        return IdentityResolution(None, None, ("no explicit accepted identity exists",))
    if authorization.decision_type is DecisionType.MANUAL_IDENTITY:
        identity = _manual_identity(authorization, classified_kind)
    else:
        try:
            identity = _candidate_identity(authorization)
        except ValueError as exc:
            return IdentityResolution(None, authorization, (str(exc),))
    identity = _apply_overrides(identity, repository.manual_overrides(source))
    return IdentityResolution(identity, authorization, identity.planning_blocks())


def _candidate_identity(decision: DecisionRecord) -> CanonicalIdentity:
    value = decision.payload.get("candidate")
    if not isinstance(value, dict):
        raise ValueError("accepted decision has no resolved candidate snapshot")
    candidate = NormalizedCandidate.from_dict(value)
    if candidate.record_type is RecordType.COMIC_RUN:
        raise ValueError("accepted comic run record cannot resolve a source-media identity")
    work_only = (
        decision.decision_type is DecisionType.WORK_ACCEPTED
        or candidate.record_type is RecordType.BOOK_WORK
    )
    contributors = _contributor_groups(candidate)
    creators = (
        contributors.get("writers", ())
        if candidate.media_kind is MediaKind.COMIC
        else contributors.get("authors", ())
    )
    identifiers = {} if work_only else {item.scheme: item.value for item in candidate.identifiers}
    return CanonicalIdentity(
        media_kind=candidate.media_kind,
        title=candidate.title,
        creators=creators,
        series_title=candidate.series_title,
        sequence=candidate.sequence,
        run_start_year=candidate.run_start_year,
        item_type=candidate.item_type,
        publisher=None if work_only else candidate.publisher,
        publication_date=None if work_only else candidate.publication_date,
        release_date=None if work_only else candidate.release_date,
        release_date_precision=None if work_only else candidate.release_date_precision,
        cover_date=None if work_only else candidate.cover_date,
        cover_date_precision=None if work_only else candidate.cover_date_precision,
        language=None if work_only else candidate.language,
        identifiers=identifiers,
        contributors=contributors,
        provider_identity={
            "provider": candidate.provider.value,
            "provider_id": candidate.provider_id,
            "record_type": candidate.record_type.value,
            **({"run_id": candidate.run_id} if candidate.run_id else {}),
        },
        resolution=ResolutionLevel.WORK_ONLY if work_only else ResolutionLevel.COMPLETE,
        provenance={
            "decision_id": str(decision.id),
            "decision_type": decision.decision_type.value,
            **(
                {"provider_format": candidate.provider_metadata["raw_format"]}
                if candidate.provider_metadata.get("raw_format")
                else {}
            ),
            **{
                key: value
                for key, value in candidate.provider_metadata.items()
                if key in {"release_date_source", "cover_date_source", "cover_date_precision"}
            },
        },
    )


def _manual_identity(decision: DecisionRecord, kind: MediaKind) -> CanonicalIdentity:
    fields = decision.payload.get("identity")
    if not isinstance(fields, dict):
        raise ValueError("manual identity decision has no identity snapshot")
    sequence = SequenceNumber.parse(str(fields["sequence"])) if fields.get("sequence") else None
    creators = _strings(fields.get("authors") or fields.get("creators"))
    return CanonicalIdentity(
        media_kind=kind,
        title=str(fields.get("title") or ""),
        creators=creators,
        series_title=_optional_string(fields.get("series_title")),
        sequence=sequence,
        item_type=_optional_string(fields.get("item_type")),
        run_start_year=_optional_int(fields.get("run_start_year")),
        collection_volume=_optional_int(fields.get("collection_volume")),
        publisher=_optional_string(fields.get("publisher")),
        publication_date=_optional_string(fields.get("publication_date")),
        release_date=_optional_string(fields.get("release_date")),
        release_date_precision=_optional_string(fields.get("release_date_precision")),
        cover_date=_optional_string(fields.get("cover_date")),
        cover_date_precision=_optional_string(fields.get("cover_date_precision")),
        language=_optional_string(fields.get("language")),
        identifiers={key: str(value) for key, value in fields.items() if key.startswith("isbn")},
        resolution=ResolutionLevel.MANUAL,
        provenance={"decision_id": str(decision.id), "decision_type": decision.decision_type.value},
    )


def _apply_overrides(identity: CanonicalIdentity, values: dict[str, Any]) -> CanonicalIdentity:
    updates: dict[str, Any] = {}
    identifiers = dict(identity.identifiers)
    contributors = dict(identity.contributors)
    mapping = {
        "authors": "creators",
        "series_index": "sequence",
        "date": "publication_date",
    }
    allowed = {
        "title",
        "series_title",
        "publisher",
        "publication_date",
        "release_date",
        "release_date_precision",
        "cover_date",
        "cover_date_precision",
        "language",
        "item_type",
        "run_start_year",
        "collection_volume",
        "creators",
        "sequence",
    }
    for field, value in values.items():
        if field in {"isbn", "isbn10", "isbn13"}:
            identifiers[field] = str(value)
            continue
        if field in {"translators", "editors", "illustrators"}:
            contributors[field] = _strings(value)
            continue
        target = mapping.get(field, field)
        if target not in allowed:
            continue
        if target == "sequence":
            updates[target] = SequenceNumber.parse(str(value))
        elif target == "creators":
            updates[target] = _strings(value)
        else:
            updates[target] = value
    if identifiers != identity.identifiers:
        updates["identifiers"] = identifiers
    if contributors != identity.contributors:
        updates["contributors"] = contributors
    return replace(identity, **updates)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


_CONTRIBUTOR_GROUPS = {
    "author": "authors",
    "writer": "writers",
    "script": "writers",
    "penciler": "pencillers",
    "penciller": "pencillers",
    "inker": "inkers",
    "colorist": "colorists",
    "colourist": "colorists",
    "letterer": "letterers",
    "cover": "cover_artists",
    "cover-artist": "cover_artists",
    "cover artist": "cover_artists",
    "editor": "editors",
    "translator": "translators",
}


def _contributor_groups(candidate: NormalizedCandidate) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for contributor in candidate.creators:
        role = contributor.role.strip().casefold()
        group = _CONTRIBUTOR_GROUPS.get(
            role,
            role if role.startswith("unknown:") else f"unknown:{role or 'creator'}",
        )
        grouped.setdefault(group, []).append(contributor.name)
    return {key: tuple(dict.fromkeys(names)) for key, names in grouped.items()}


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return int(value)
    raise ValueError(f"expected an integer-compatible value, got {type(value).__name__}")
