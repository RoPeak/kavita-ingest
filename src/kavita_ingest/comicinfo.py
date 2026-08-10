from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from typing import Any

from lxml import etree

OWNED_FIELDS = (
    "Title",
    "Series",
    "Number",
    "Volume",
    "Format",
    "Year",
    "Month",
    "Day",
    "Writer",
    "Penciller",
    "Inker",
    "Colorist",
    "Letterer",
    "CoverArtist",
    "Editor",
    "Publisher",
    "Imprint",
    "LanguageISO",
    "GTIN",
)


class ComicInfoError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ComicInfoDocument:
    metadata: dict[str, Any]
    schema_valid: bool


def read_comicinfo(data: bytes, *, require_schema: bool = False) -> ComicInfoDocument:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise ComicInfoError(f"invalid ComicInfo XML: {exc}") from exc
    if etree.QName(root).localname != "ComicInfo":
        raise ComicInfoError("ComicInfo root element is required")
    metadata: dict[str, Any] = {}
    for field in OWNED_FIELDS:
        nodes = root.xpath(f"./*[local-name()='{field}']")
        if len(nodes) > 1:
            raise ComicInfoError(f"ambiguous duplicate ComicInfo field: {field}")
        if nodes and nodes[0].text is not None:
            metadata[field] = nodes[0].text.strip()
    valid = _schema().validate(root)
    if require_schema and not valid:
        error = _schema().error_log.last_error
        raise ComicInfoError(f"ComicInfo 2.1 schema validation failed: {error}")
    return ComicInfoDocument(metadata, valid)


def _schema() -> etree.XMLSchema:
    resource = importlib.resources.files("kavita_ingest").joinpath("schemas/ComicInfo-2.1.xsd")
    return etree.XMLSchema(etree.parse(str(resource)))
