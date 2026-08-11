from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

from ..domain import MediaKind, SequenceNumber
from .base import ProviderStatus
from .client import CachedProviderClient
from .models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)

NORMALIZATION_SCHEMA_VERSION = 3


class ComicVineProvider:
    name = ProviderName.COMIC_VINE
    normalization_schema_version = NORMALIZATION_SCHEMA_VERSION
    endpoint = "https://comicvine.gamespot.com/api"

    def __init__(self, client: CachedProviderClient, api_key: str | None) -> None:
        self.client = client
        self.api_key = api_key

    def status(self) -> ProviderStatus:
        enabled = bool(self.api_key)
        return ProviderStatus(
            self.name,
            enabled,
            True,
            enabled,
            "API key configured" if enabled else "COMIC_VINE_API_KEY is missing",
            ("structured_search", "exact_fetch", "comic_run", "comic_issue", "cached"),
        )

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        title = query.series_title or query.title
        terms = [title]
        if query.sequence:
            terms.append(query.sequence.raw)
        collected = query.item_type == "collected-edition"
        if collected:
            terms.append("TPB")
        params = {
            "query": " ".join(terms),
            "resources": "issue,volume",
            "limit": "10",
            "field_list": (
                "id,resource_type,api_detail_url,name,issue_number,cover_date,"
                "store_date,volume,person_credits,format"
            ),
        }
        return self.client.get(
            "search",
            f"{self.endpoint}/search/",
            params,
            self._secret(),
            "search:collection" if collected else "search:issue",
            _normalize,
        )

    def search_runs(self, query: SearchQuery) -> list[NormalizedCandidate]:
        return self.client.get(
            "search-runs",
            f"{self.endpoint}/search/",
            {
                "query": query.series_title or query.title,
                "resources": "volume",
                "limit": "10",
                "field_list": "id,resource_type,api_detail_url,name,start_year,publisher",
            },
            self._secret(),
            "search:run",
            _normalize,
        )

    def search_issue_in_run(
        self,
        run: NormalizedCandidate,
        sequence: SequenceNumber,
    ) -> list[NormalizedCandidate]:
        volume_id = run.provider_id.split("-", 1)[-1]
        candidates = self.client.get(
            "issues-in-run",
            f"{self.endpoint}/issues/",
            {
                "filter": f"volume:{volume_id},issue_number:{sequence.normalized}",
                "limit": "10",
                "field_list": (
                    "id,resource_type,api_detail_url,name,issue_number,cover_date,"
                    "store_date,volume,person_credits,format"
                ),
            },
            self._secret(),
            "issues",
            _normalize,
        )
        return [
            replace(
                candidate,
                run_id=run.provider_id,
                run_start_year=run.run_start_year,
                publisher=candidate.publisher or run.publisher,
            )
            for candidate in candidates
        ]

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        prefix = provider_id.split("-", 1)[0]
        resource = "issue" if prefix == "4000" else "volume"
        return self.client.get(
            "fetch",
            f"{self.endpoint}/{resource}/{provider_id}/",
            {},
            self._secret(),
            resource,
            _normalize,
        )

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        if identifier.scheme.casefold() not in {"comicvine", "comic_vine"}:
            return []
        return self.fetch(identifier.value)

    def _secret(self) -> dict[str, str]:
        values = {"format": "json"}
        if self.api_key:
            values["api_key"] = self.api_key
        return values


_ITEM_TYPES = {
    "annual": "annual",
    "one shot": "one-shot",
    "oneshot": "one-shot",
    "special": "special",
    "tpb": "collected-edition",
    "trade paperback": "collected-edition",
    "hardcover": "collected-edition",
    "omnibus": "omnibus",
    "graphic novel": "graphic-novel",
}
_COLLECTION_TYPES = {"collected-edition", "omnibus", "graphic-novel"}
_CREDIT_ROLES = {
    "writer": "writer",
    "script": "writer",
    "penciler": "penciller",
    "penciller": "penciller",
    "inker": "inker",
    "colorist": "colorist",
    "colourist": "colorist",
    "letterer": "letterer",
    "cover": "cover-artist",
    "cover artist": "cover-artist",
    "editor": "editor",
    "translator": "translator",
    "artist": "artist",
}


def _normalize(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict) or "results" not in raw:
        raise ValueError("Comic Vine response requires results")
    results = raw["results"]
    items = results if isinstance(results, list) else [results]
    output: list[NormalizedCandidate] = []
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        resource = str(item.get("resource_type", ""))
        api_url = str(item.get("api_detail_url", ""))
        is_issue = resource == "issue" or "/issue/" in api_url
        volume_raw = item.get("volume")
        volume: dict[str, object] = volume_raw if isinstance(volume_raw, dict) else {}
        series_title = str(volume.get("name") or item.get("name") or "").strip()
        issue_name = str(item.get("name") or "").strip()
        if not series_title:
            continue
        number = str(item.get("issue_number") or "").strip()
        format_value = str(item.get("format") or "").strip()
        item_type = _item_type(format_value, is_issue)
        collection = item_type in _COLLECTION_TYPES
        provider_id = _api_id(api_url) or (f"{'4000' if is_issue else '4050'}-{item['id']}")
        start_year = _year(item.get("start_year")) or _year(volume.get("start_year"))
        release_date = _exact_date(item.get("store_date")) if is_issue else None
        cover_date, cover_precision = (
            _cover_date(item.get("cover_date"))
            if is_issue
            else (
                None,
                None,
            )
        )
        date_provenance = {}
        if release_date:
            date_provenance["release_date_source"] = "store_date"
        if cover_date:
            date_provenance["cover_date_source"] = "cover_date"
            date_provenance["cover_date_precision"] = cover_precision or ""
        output.append(
            NormalizedCandidate(
                ProviderName.COMIC_VINE,
                provider_id,
                RecordType.COMIC_COLLECTION
                if collection
                else (RecordType.COMIC_ISSUE if is_issue else RecordType.COMIC_RUN),
                MediaKind.COMIC,
                issue_name or series_title,
                creators=_credits(item.get("person_credits")),
                identifiers=(Identifier("comic_vine", provider_id),),
                publisher=_publisher(item, volume),
                release_date=release_date,
                release_date_precision="day" if release_date else None,
                cover_date=cover_date,
                cover_date_precision=cover_precision,
                series_title=series_title,
                run_start_year=start_year,
                sequence=SequenceNumber.parse(number) if number else None,
                item_type=item_type,
                run_id=_volume_id(volume) if is_issue else provider_id,
                provider_metadata={
                    **({"raw_format": format_value} if format_value else {}),
                    **date_provenance,
                },
                provider_schema_version=NORMALIZATION_SCHEMA_VERSION,
            )
        )
    return output


def _api_id(url: str) -> str | None:
    match = re.search(r"/(?:issue|volume)/(\d+-\d+)/", url)
    return match.group(1) if match else None


def _volume_id(volume: dict[str, object]) -> str | None:
    api_id = _api_id(str(volume.get("api_detail_url") or ""))
    if api_id:
        return api_id
    value = volume.get("id")
    return f"4050-{value}" if value is not None else None


def _year(value: object) -> int | None:
    text = str(value or "")
    return int(text[:4]) if re.fullmatch(r"\d{4}.*", text) else None


def _exact_date(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _cover_date(value: object) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return (text, "year") if int(text) > 0 else (None, None)
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if not match:
        return None, None
    year, month = (int(part) for part in match.groups()[:2])
    if year <= 0 or not 1 <= month <= 12:
        return None, None
    day = match.group(3)
    if day:
        try:
            date(year, month, int(day))
        except ValueError:
            return None, None
    return f"{year:04d}-{month:02d}", "month"


def _publisher(item: dict[str, object], volume: dict[str, object]) -> str | None:
    raw = item.get("publisher") or volume.get("publisher")
    return str(raw.get("name")) if isinstance(raw, dict) and raw.get("name") else None


def _credits(value: object) -> tuple[Contributor, ...]:
    if not isinstance(value, list):
        return ()
    output = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            raw_role = str(item.get("role") or "creator").strip()
            for token in raw_role.split(","):
                normalized = _normalized_label(token)
                role = _CREDIT_ROLES.get(normalized) or f"unknown:{normalized or 'creator'}"
                key = (str(item["name"]).casefold(), role)
                if key not in seen:
                    seen.add(key)
                    output.append(
                        Contributor(
                            str(item["name"]),
                            role,
                        )
                    )
    return tuple(output)


def _item_type(format_value: str, is_issue: bool) -> str:
    if not format_value:
        return "issue" if is_issue else "run"
    return _ITEM_TYPES.get(_normalized_label(format_value), "unsupported")


def _normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
