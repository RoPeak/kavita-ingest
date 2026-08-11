from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from typing import Any, cast

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

SCHEMA_ORDER = (
    "Title",
    "Series",
    "Number",
    "Count",
    "Volume",
    "AlternateSeries",
    "AlternateNumber",
    "AlternateCount",
    "Summary",
    "Notes",
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
    "Translator",
    "Publisher",
    "Imprint",
    "Genre",
    "Tags",
    "Web",
    "PageCount",
    "LanguageISO",
    "Format",
    "BlackAndWhite",
    "Manga",
    "Characters",
    "Teams",
    "Locations",
    "ScanInformation",
    "StoryArc",
    "StoryArcNumber",
    "SeriesGroup",
    "AgeRating",
    "Pages",
    "CommunityRating",
    "MainCharacterOrTeam",
    "Review",
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


def patch_comicinfo(
    data: bytes,
    *,
    set_fields: dict[str, Any],
    clear_fields: tuple[str, ...] = (),
    require_schema: bool = True,
) -> bytes:
    """Patch owned fields while retaining every unowned node and attribute."""
    invalid = (set(set_fields) | set(clear_fields)) - set(OWNED_FIELDS)
    if invalid:
        raise ComicInfoError(f"unsupported owned ComicInfo fields: {sorted(invalid)}")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise ComicInfoError(f"invalid ComicInfo XML: {exc}") from exc
    if etree.QName(root).localname != "ComicInfo":
        raise ComicInfoError("ComicInfo root element is required")
    for field in OWNED_FIELDS:
        nodes = root.xpath(f"./*[local-name()='{field}']")
        if len(nodes) > 1:
            raise ComicInfoError(f"ambiguous duplicate ComicInfo field: {field}")
    for field in clear_fields:
        for node in root.xpath(f"./*[local-name()='{field}']"):
            root.remove(node)
    positions = {name: index for index, name in enumerate(SCHEMA_ORDER)}
    for field, value in set_fields.items():
        nodes = root.xpath(f"./*[local-name()='{field}']")
        node = nodes[0] if nodes else etree.Element(field)
        node.text = str(value)
        if not nodes:
            insertion = len(root)
            for index, sibling in enumerate(root):
                sibling_position = positions.get(etree.QName(sibling).localname)
                if sibling_position is not None and sibling_position > positions[field]:
                    insertion = index
                    break
            root.insert(insertion, node)
    if require_schema and not _schema().validate(root):
        error = _schema().error_log.last_error
        raise ComicInfoError(f"ComicInfo 2.1 schema validation failed: {error}")
    return cast(bytes, etree.tostring(root, encoding="utf-8", xml_declaration=True))


def _schema() -> etree.XMLSchema:
    resource = importlib.resources.files("kavita_ingest").joinpath("schemas/ComicInfo-2.1.xsd")
    return etree.XMLSchema(etree.parse(str(resource)))
