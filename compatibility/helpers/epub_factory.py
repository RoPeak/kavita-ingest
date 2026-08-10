from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

from lxml import etree

CONTAINER_PATH = "META-INF/container.xml"
OPF_PATH = "OEBPS/package.opf"
NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf",
}

MIMETYPE = b"application/epub+zip"
CONTAINER = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:fixture="https://example.invalid/fixture" version="3.0" unique-identifier="pub-id">
  <metadata>
    <dc:identifier id="pub-id">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
    <dc:identifier id="isbn-id">urn:isbn:9780000000002</dc:identifier>
    <dc:title id="title-id">Fixture Book</dc:title>
    <dc:creator id="creator-author">Alex Author</dc:creator>
    <meta refines="#creator-author" property="role" scheme="marc:relators">aut</meta>
    <dc:creator id="creator-translator">Terry Translator</dc:creator>
    <meta refines="#creator-translator" property="role" scheme="marc:relators">trl</meta>
    <dc:creator id="creator-editor">Eddie Editor</dc:creator>
    <meta refines="#creator-editor" property="role" scheme="marc:relators">edt</meta>
    <dc:creator id="creator-illustrator">Indigo Illustrator</dc:creator>
    <meta refines="#creator-illustrator" property="role" scheme="marc:relators">ill</meta>
    <dc:language>en-GB</dc:language>
    <dc:publisher>Fixture Press</dc:publisher>
    <dc:date>2024-03-14</dc:date>
    <dc:description>Original description</dc:description>
    <dc:subject>Testing</dc:subject>
    <meta name="calibre:series" content="Fixture Series"/>
    <meta name="calibre:series_index" content="1.5"/>
    <meta name="cover" content="cover-image"/>
    <meta property="fixture:keep">preserve-me</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="cover-image" href="cover.png" media-type="image/png" properties="cover-image"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""

RESOURCES = {
    "OEBPS/nav.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'><body><nav><ol><li><a href='chapter.xhtml'>Chapter</a></li></ol></nav></body></html>",
    "OEBPS/chapter.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>Fixture</h1><p>Publication content.</p></body></html>",
    "OEBPS/style.css": b"body { font-family: serif; }\n",
    "OEBPS/cover.png": b"\x89PNG\r\n\x1a\nfixture-cover-payload",
}


def create_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr(CONTAINER_PATH, CONTAINER)
        archive.writestr(OPF_PATH, OPF)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
    return path


def copy_epub(source: Path, destination: Path) -> Path:
    shutil.copy2(source, destination)
    return destination


def read_opf(epub: Path) -> tuple[etree._Element, bytes]:
    with zipfile.ZipFile(epub) as archive:
        names = archive.namelist()
        assert names[0] == "mimetype"
        assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == MIMETYPE
        container = etree.fromstring(archive.read(CONTAINER_PATH))
        rootfile = container.xpath("string(//container:rootfile/@full-path)", namespaces=NS)
        payload = archive.read(rootfile)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    return etree.fromstring(payload, parser=parser), payload


def publication_hashes(epub: Path) -> dict[str, str]:
    with zipfile.ZipFile(epub) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name != OPF_PATH
        }


def opf_snapshot(epub: Path) -> dict[str, object]:
    root, _ = read_opf(epub)
    metadata = root.find("opf:metadata", NS)
    assert metadata is not None
    creator_lists: dict[str, list[str]] = {}
    creator_ids: dict[str, str] = {}
    for creator in metadata.findall("dc:creator", NS):
        creator_id = creator.get("id")
        role = creator.get(f"{{{NS['opf']}}}role", "")
        if creator_id and not role:
            role = metadata.xpath(
                "string(opf:meta[@refines=$ref and @property='role'][1])",
                namespaces=NS,
                ref=f"#{creator_id}",
            )
        resolved_role = str(role or "aut")
        creator_lists.setdefault(resolved_role, []).append(str(creator.text or ""))
        if creator_id:
            creator_ids[str(creator.text or "")] = creator_id
    named_meta = {
        str(node.get("name")): str(node.get("content"))
        for node in metadata.findall("opf:meta", NS)
        if node.get("name")
    }
    collection: list[etree._Element] = []
    for node in metadata.findall("opf:meta", NS):
        if node.get("property") != "belongs-to-collection" or not node.get("id"):
            continue
        collection_type = metadata.xpath(
            "string(opf:meta[@refines=$ref and @property='collection-type'][1])",
            namespaces=NS,
            ref=f"#{node.get('id')}",
        )
        if collection_type == "series":
            collection.append(node)
    series = named_meta.get("calibre:series")
    series_index = named_meta.get("calibre:series_index")
    if collection:
        series = str(collection[0].text or "")
        collection_id = collection[0].get("id")
        if collection_id:
            series_index = str(
                metadata.xpath(
                    "string(opf:meta[@refines=$ref and @property='group-position'][1])",
                    namespaces=NS,
                    ref=f"#{collection_id}",
                )
                or ""
            )
    manifest_items = sorted(
        (
            str(node.get("id")),
            str(node.get("href")),
            str(node.get("media-type")),
            str(node.get("properties") or ""),
        )
        for node in root.findall("opf:manifest/opf:item", NS)
    )
    return {
        "title": root.xpath("string(opf:metadata/dc:title[1])", namespaces=NS),
        "creators": {role: names[0] for role, names in creator_lists.items()},
        "creator_lists": creator_lists,
        "creator_ids": creator_ids,
        "publisher": root.xpath("string(opf:metadata/dc:publisher[1])", namespaces=NS),
        "date": root.xpath("string(opf:metadata/dc:date[1])", namespaces=NS),
        "language": root.xpath("string(opf:metadata/dc:language[1])", namespaces=NS),
        "identifiers": root.xpath("opf:metadata/dc:identifier/text()", namespaces=NS),
        "description": root.xpath("string(opf:metadata/dc:description[1])", namespaces=NS),
        "subjects": root.xpath("opf:metadata/dc:subject/text()", namespaces=NS),
        "series": series,
        "series_index": series_index,
        "unique_identifier": root.get("unique-identifier"),
        "manifest": etree.tostring(root.find("opf:manifest", NS)),
        "manifest_items": manifest_items,
        "spine": etree.tostring(root.find("opf:spine", NS)),
        "spine_ids": root.xpath("opf:spine/opf:itemref/@idref", namespaces=NS),
        "cover": named_meta.get("cover"),
        "custom_meta": root.xpath(
            "string(opf:metadata/opf:meta[@property='fixture:keep'][1])", namespaces=NS
        ),
    }


def validate_epub(epub: Path) -> None:
    root, _ = read_opf(epub)
    with zipfile.ZipFile(epub) as archive:
        bad = archive.testzip()
        assert bad is None
        manifest_hrefs = root.xpath("opf:manifest/opf:item/@href", namespaces=NS)
        for href in manifest_hrefs:
            assert f"OEBPS/{href}" in archive.namelist()
