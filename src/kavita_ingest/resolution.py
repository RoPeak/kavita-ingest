from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .canonical import CanonicalIdentity, ResolutionLevel
from .decisions import DecisionRecord, DecisionRepository, DecisionType
from .domain import MediaKind, SequenceNumber, SourceRecord
from .providers.models import NormalizedCandidate


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
        identity = _candidate_identity(authorization)
    identity = _apply_overrides(identity, repository.manual_overrides(source))
    return IdentityResolution(identity, authorization, identity.planning_blocks())


def _candidate_identity(decision: DecisionRecord) -> CanonicalIdentity:
    value = decision.payload.get("candidate")
    if not isinstance(value, dict):
        raise ValueError("accepted decision has no resolved candidate snapshot")
    candidate = NormalizedCandidate.from_dict(value)
    work_only = decision.decision_type is DecisionType.WORK_ACCEPTED
    identifiers = {} if work_only else {item.scheme: item.value for item in candidate.identifiers}
    return CanonicalIdentity(
        media_kind=candidate.media_kind,
        title=candidate.title,
        creators=tuple(item.name for item in candidate.creators if item.role == "author"),
        series_title=candidate.series_title,
        sequence=candidate.sequence,
        run_start_year=candidate.run_start_year,
        item_type=candidate.item_type,
        publisher=None if work_only else candidate.publisher,
        publication_date=None if work_only else candidate.publication_date,
        language=None if work_only else candidate.language,
        identifiers=identifiers,
        provider_identity={
            "provider": candidate.provider.value,
            "provider_id": candidate.provider_id,
            "record_type": candidate.record_type.value,
        },
        resolution=ResolutionLevel.WORK_ONLY if work_only else ResolutionLevel.COMPLETE,
        provenance={"decision_id": str(decision.id), "decision_type": decision.decision_type.value},
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
        publisher=_optional_string(fields.get("publisher")),
        publication_date=_optional_string(fields.get("publication_date")),
        language=_optional_string(fields.get("language")),
        identifiers={key: str(value) for key, value in fields.items() if key.startswith("isbn")},
        resolution=ResolutionLevel.MANUAL,
        provenance={"decision_id": str(decision.id), "decision_type": decision.decision_type.value},
    )


def _apply_overrides(identity: CanonicalIdentity, values: dict[str, Any]) -> CanonicalIdentity:
    updates: dict[str, Any] = {}
    mapping = {"authors": "creators", "series_index": "sequence"}
    allowed = {
        "title",
        "series_title",
        "publisher",
        "publication_date",
        "language",
        "item_type",
        "creators",
        "sequence",
    }
    for field, value in values.items():
        target = mapping.get(field, field)
        if target not in allowed:
            continue
        if target == "sequence":
            updates[target] = SequenceNumber.parse(str(value))
        elif target == "creators":
            updates[target] = _strings(value)
        else:
            updates[target] = value
    return replace(identity, **updates)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
