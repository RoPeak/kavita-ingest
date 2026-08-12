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
    "Translator",
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
        nodes = _field_nodes(root, field)
        if len(nodes) > 1:
            raise ComicInfoError(f"ambiguous duplicate ComicInfo field: {field}")
        if nodes and nodes[0].text is not None:
            metadata[field] = nodes[0].text.strip()
    schema = _schema()
    valid = schema.validate(root)
    if require_schema and not valid:
        raise ComicInfoError(_validation_message(schema))
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
    _normalize_legacy_issue_alias(root, set_fields=set_fields)
    for field in OWNED_FIELDS:
        nodes = _field_nodes(root, field)
        if len(nodes) > 1:
            raise ComicInfoError(f"ambiguous duplicate ComicInfo field: {field}")
    for field in clear_fields:
        for node in _field_nodes(root, field):
            root.remove(node)
    for field, value in set_fields.items():
        nodes = _field_nodes(root, field)
        node = nodes[0] if nodes else etree.Element(field)
        node.text = str(value)
        if not nodes:
            root.append(node)
    _order_known_elements(root)
    if require_schema:
        schema = _schema()
        if not schema.validate(root):
            raise ComicInfoError(_validation_message(schema))
    return cast(bytes, etree.tostring(root, encoding="utf-8", xml_declaration=True))


def _normalize_legacy_issue_alias(
    root: etree._Element,
    *,
    set_fields: dict[str, Any],
) -> None:
    """Remove a redundant unnamespaced <Issue> alias when Number is authoritative."""
    nodes = _field_nodes(root, "Issue")

    if not nodes:
        return

    if len(nodes) > 1:
        raise ComicInfoError(
            "ambiguous duplicate legacy ComicInfo field: Issue"
        )

    if "Number" not in set_fields:
        return

    node = nodes[0]
    legacy_value = (node.text or "").strip()
    planned_value = str(set_fields["Number"]).strip()

    equivalent = legacy_value == planned_value

    if (
        not equivalent
        and legacy_value.isdigit()
        and planned_value.isdigit()
    ):
        equivalent = int(legacy_value) == int(planned_value)

    if legacy_value and not equivalent:
        raise ComicInfoError(
            "legacy ComicInfo Issue value "
            f"{legacy_value!r} conflicts with planned Number "
            f"{planned_value!r}"
        )

    root.remove(node)


def _field_nodes(root: etree._Element, field: str) -> list[etree._Element]:
    return [child for child in root if _schema_name(child) == field]


def _schema_name(node: etree._Element) -> str | None:
    if not isinstance(node.tag, str):
        return None
    name = etree.QName(node)
    return name.localname if name.namespace in {None, ""} else None


def _order_known_elements(root: etree._Element) -> None:
    positions = {name: index for index, name in enumerate(SCHEMA_ORDER)}
    children = list(root)
    ordered = sorted(
        enumerate(children),
        key=lambda item: (
            0,
            positions[cast(str, _schema_name(item[1]))],
            item[0],
        )
        if _schema_name(item[1]) in positions
        else (1, item[0], item[0]),
    )
    for child in children:
        root.remove(child)
    for _, child in ordered:
        root.append(child)


def _validation_message(schema: etree.XMLSchema) -> str:
    errors = list(schema.error_log)
    if not errors:
        return "ComicInfo 2.1 schema validation failed without validator diagnostics"
    details = []
    for error in errors:
        location = f"line {error.line}" if error.line else "line unavailable"
        classification = "/".join(
            value for value in (error.domain_name, error.type_name) if value
        )
        suffix = f", {classification}" if classification else ""
        details.append(f"{error.message} ({location}{suffix})")
    return "ComicInfo 2.1 schema validation failed: " + "; ".join(details)


def _schema() -> etree.XMLSchema:
    resource = importlib.resources.files("kavita_ingest").joinpath("schemas/ComicInfo-2.1.xsd")
    return etree.XMLSchema(etree.parse(str(resource)))
