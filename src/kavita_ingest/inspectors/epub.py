from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ..archive_safety import ArchiveLimits, UnsafeArchive, validate_inventory
from ..domain import Evidence, InspectionResult, InspectionStatus, SourceFormat

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XML_METADATA_MAX_BYTES = 8 * 1024 * 1024


def inspect_epub(path: Path, limits: ArchiveLimits) -> InspectionResult:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            validate_inventory(
                members, limits, encrypted_names={i.filename for i in members if i.flag_bits & 1}
            )
            if (
                "mimetype" not in archive.namelist()
                or archive.read("mimetype") != b"application/epub+zip"
            ):
                raise ValueError("EPUB mimetype entry is missing or invalid")
            _require_small_entry(archive, "META-INF/container.xml")
            container = _xml(archive.read("META-INF/container.xml"))
            roots = container.xpath("//container:rootfile/@full-path", namespaces=NS)
            if len(roots) != 1:
                raise ValueError("EPUB must identify exactly one package document")
            _require_small_entry(archive, str(roots[0]))
            opf = _xml(archive.read(str(roots[0])))
            metadata = _metadata(opf)
        evidence = tuple(
            Evidence(key, str(value), str(value), "epub-opf", 0.98)
            for key, value in metadata.items()
            if value and not isinstance(value, list)
        )
        return InspectionResult(InspectionStatus.OK, SourceFormat.EPUB, metadata, evidence)
    except (
        OSError,
        KeyError,
        ValueError,
        zipfile.BadZipFile,
        etree.XMLSyntaxError,
        UnsafeArchive,
    ) as exc:
        return InspectionResult(
            InspectionStatus.FAILED,
            SourceFormat.EPUB,
            error_code="invalid_epub",
            error_message=str(exc),
        )


def _xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def _require_small_entry(archive: zipfile.ZipFile, name: str) -> None:
    if archive.getinfo(name).file_size > XML_METADATA_MAX_BYTES:
        raise ValueError(f"EPUB metadata entry exceeds size limit: {name}")


def _metadata(opf: etree._Element) -> dict[str, object]:
    values: dict[str, object] = {}
    singular = {
        "title": "string(//dc:title[1])",
        "publisher": "string(//dc:publisher[1])",
        "date": "string(//dc:date[1])",
        "language": "string(//dc:language[1])",
        "description": "string(//dc:description[1])",
    }
    for key, expression in singular.items():
        value = str(opf.xpath(expression, namespaces=NS)).strip()
        if value:
            values[key] = value
    contributors = _contributors(opf)
    values["creators"] = contributors.get("aut", [])
    values["contributors"] = contributors
    values["subjects"] = [
        str(item).strip() for item in opf.xpath("//dc:subject/text()", namespaces=NS)
    ]
    values["identifiers"] = [
        str(item).strip() for item in opf.xpath("//dc:identifier/text()", namespaces=NS)
    ]
    legacy_series = opf.xpath("//opf:meta[@name='calibre:series']/@content", namespaces=NS)
    legacy_index = opf.xpath("//opf:meta[@name='calibre:series_index']/@content", namespaces=NS)
    collection_ids = opf.xpath("//opf:meta[@property='belongs-to-collection']/@id", namespaces=NS)
    collection = opf.xpath("//opf:meta[@property='belongs-to-collection']/text()", namespaces=NS)
    series = (
        str(collection[0]).strip()
        if collection
        else (str(legacy_series[0]).strip() if legacy_series else "")
    )
    index = str(legacy_index[0]).strip() if legacy_index else ""
    if collection_ids:
        refined = opf.xpath(
            "//opf:meta[@property='group-position' and @refines=$ref]/text()",
            namespaces=NS,
            ref=f"#{collection_ids[0]}",
        )
        if refined:
            index = str(refined[0]).strip()
    if series:
        values["series"] = series
    if index:
        values["series_index"] = index
    return values


def _contributors(opf: etree._Element) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    nodes = opf.xpath("//dc:creator | //dc:contributor", namespaces=NS)
    for node in nodes:
        name = str(node.text or "").strip()
        if not name:
            continue
        role = node.get(f"{{{NS['opf']}}}role") or ""
        identifier = node.get("id")
        if identifier and not role:
            role = str(
                opf.xpath(
                    "string(//opf:meta[@refines=$ref and @property='role'][1])",
                    namespaces=NS,
                    ref=f"#{identifier}",
                )
            )
        output.setdefault(role or "aut", []).append(name)
    return output
