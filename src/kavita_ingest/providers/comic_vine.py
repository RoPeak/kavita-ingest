from __future__ import annotations

import re

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


class ComicVineProvider:
    name = ProviderName.COMIC_VINE
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


_COLLECTION_FORMATS = {"tpb", "trade paperback", "hardcover", "omnibus", "graphic novel"}


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
        collection = format_value.casefold() in _COLLECTION_FORMATS
        provider_id = _api_id(api_url) or (f"{'4000' if is_issue else '4050'}-{item['id']}")
        start_year = _year(item.get("start_year")) or _year(volume.get("start_year"))
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
                publication_date=_date(item),
                series_title=series_title,
                run_start_year=start_year,
                sequence=SequenceNumber.parse(number) if number else None,
                item_type=format_value or ("issue" if is_issue else "run"),
            )
        )
    return output


def _api_id(url: str) -> str | None:
    match = re.search(r"/(?:issue|volume)/(\d+-\d+)/", url)
    return match.group(1) if match else None


def _year(value: object) -> int | None:
    text = str(value or "")
    return int(text[:4]) if re.fullmatch(r"\d{4}.*", text) else None


def _date(item: dict[str, object]) -> str | None:
    for key in ("cover_date", "store_date", "date_added"):
        if item.get(key):
            return str(item[key])
    return None


def _publisher(item: dict[str, object], volume: dict[str, object]) -> str | None:
    raw = item.get("publisher") or volume.get("publisher")
    return str(raw.get("name")) if isinstance(raw, dict) and raw.get("name") else None


def _credits(value: object) -> tuple[Contributor, ...]:
    if not isinstance(value, list):
        return ()
    output = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            output.append(Contributor(str(item["name"]), str(item.get("role") or "creator")))
    return tuple(output)
