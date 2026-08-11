from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from lxml import etree

from .common import VerificationResult

CONTAINER = "META-INF/container.xml"
CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf",
}
OWNABLE_ROLES = frozenset({"trl", "edt", "ill", "clr"})
CALIBRE_FIELDS = frozenset(
    {
        "title",
        "authors",
        "publisher",
        "language",
        "identifiers",
        "description",
        "subjects",
        "series",
        "series_index",
    }
)


def write_epub(
    source: Path,
    destination: Path,
    *,
    calibre_fields: Mapping[str, object],
    exact_date: str | None = None,
    contributor_roles: Mapping[str, Sequence[str]] | None = None,
    ebook_meta: str = "ebook-meta",
) -> VerificationResult:
    unsupported = set(calibre_fields) - CALIBRE_FIELDS
    if unsupported:
        raise ValueError(f"unsupported Calibre-owned fields: {sorted(unsupported)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".epub", dir=destination.parent
    )
    os.close(fd)
    staged = Path(name)
    try:
        shutil.copy2(source, staged)
        if calibre_fields:
            subprocess.run(
                _ebook_meta_command(ebook_meta, staged, calibre_fields),
                check=True,
                capture_output=True,
                text=True,
            )
        if exact_date is not None or contributor_roles:
            _patch_opf(staged, exact_date=exact_date, contributor_roles=contributor_roles or {})
        result = verify_epub(source, staged, calibre_fields, exact_date, contributor_roles or {})
        result.require_valid()
        os.replace(staged, destination)
        return result
    finally:
        staged.unlink(missing_ok=True)


def verify_epub(
    source: Path,
    candidate: Path,
    calibre_fields: Mapping[str, object],
    exact_date: str | None,
    contributor_roles: Mapping[str, Sequence[str]],
) -> VerificationResult:
    errors: list[str] = []
    try:
        source_hashes = _resource_hashes(source)
        candidate_hashes = _resource_hashes(candidate)
        if source_hashes != candidate_hashes:
            errors.append("non-OPF publication resources changed")
        if _opf_structure(source) != _opf_structure(candidate):
            errors.append("OPF package identity, manifest, spine, navigation, or cover changed")
        owned_roles = set(contributor_roles)
        source_unowned = set(_unowned_metadata(source, owned_roles))
        candidate_unowned = set(_unowned_metadata(candidate, owned_roles))
        if not source_unowned.issubset(candidate_unowned):
            errors.append("unowned OPF metadata changed")
        root, _ = _read_opf(candidate)
        metadata = root.find("opf:metadata", NS)
        if metadata is None:
            errors.append("OPF metadata missing")
        else:
            _verify_fields(metadata, calibre_fields, exact_date, contributor_roles, errors)
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None:
                errors.append("EPUB ZIP CRC verification failed")
            first = archive.infolist()[0]
            if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
                errors.append("EPUB mimetype is not the first stored entry")
    except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        errors.append(str(exc))
    checks = (
        "zip_structure",
        "publication_resource_hashes",
        "opf_structure",
        "unowned_metadata",
        "metadata_readback",
    )
    return VerificationResult(not errors, checks, tuple(errors))


def _ebook_meta_command(executable: str, epub: Path, fields: Mapping[str, object]) -> list[str]:
    command = [executable, str(epub)]
    options = {
        "title": "--title",
        "authors": "--authors",
        "publisher": "--publisher",
        "language": "--language",
        "description": "--comments",
        "series": "--series",
        "series_index": "--index",
    }
    for field, option in options.items():
        value = fields.get(field)
        if value is not None:
            if field == "authors":
                if not isinstance(value, Sequence) or isinstance(value, str):
                    raise ValueError("authors must be a sequence of names")
                rendered = " & ".join(str(item) for item in value)
            else:
                rendered = str(value)
            command.extend([option, rendered])
    subjects = fields.get("subjects", ())
    if not isinstance(subjects, Sequence) or isinstance(subjects, str):
        raise ValueError("subjects must be a sequence")
    if subjects:
        command.extend(["--tags", ", ".join(str(subject) for subject in subjects)])
    identifiers = fields.get("identifiers", {})
    if isinstance(identifiers, Mapping):
        for key, value in identifiers.items():
            command.extend(["--identifier", f"{key}:{value}"])
    return command


def _read_opf(epub: Path) -> tuple[etree._Element, str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    with zipfile.ZipFile(epub) as archive:
        container = etree.fromstring(archive.read(CONTAINER), parser)
        opf_path = str(container.xpath("string(//c:rootfile/@full-path)", namespaces=CONTAINER_NS))
        return etree.fromstring(archive.read(opf_path), parser), opf_path


def _patch_opf(
    epub: Path, *, exact_date: str | None, contributor_roles: Mapping[str, Sequence[str]]
) -> None:
    invalid = set(contributor_roles) - OWNABLE_ROLES
    if invalid:
        raise ValueError(f"unsupported owned contributor roles: {sorted(invalid)}")
    with zipfile.ZipFile(epub) as archive:
        infos = archive.infolist()
        payloads = {info.filename: archive.read(info.filename) for info in infos}
    root, opf_path = _read_opf(epub)
    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError("EPUB OPF metadata missing")
    if exact_date is not None:
        dates = metadata.findall("dc:date", NS)
        if len(dates) > 1:
            raise ValueError("ambiguous duplicate OPF publication dates")
        date_node = dates[0] if dates else etree.Element(f"{{{NS['dc']}}}date")
        date_node.text = exact_date
        if not dates:
            metadata.append(date_node)
    _patch_roles(metadata, contributor_roles)
    payloads[opf_path] = etree.tostring(root, encoding="utf-8", xml_declaration=True)
    fd, name = tempfile.mkstemp(prefix=f".{epub.name}.", suffix=".tmp", dir=epub.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w") as target:
            for info in infos:
                target.writestr(info, payloads[info.filename])
        os.replace(temporary, epub)
    finally:
        temporary.unlink(missing_ok=True)


def _patch_roles(metadata: etree._Element, updates: Mapping[str, Sequence[str]]) -> None:
    creators = {
        node.get("id"): node for node in metadata.findall("dc:creator", NS) if node.get("id")
    }
    role_nodes: dict[str, list[etree._Element]] = {}
    for node in metadata.findall("opf:meta", NS):
        if node.get("property") == "role" and node.get("refines", "").startswith("#"):
            role_nodes.setdefault(str(node.text or ""), []).append(node)
    for role, names in updates.items():
        existing: dict[str, tuple[etree._Element, etree._Element]] = {}
        for role_node in role_nodes.get(role, []):
            creator = creators.get(role_node.get("refines", "")[1:])
            if creator is not None:
                existing[_normal(str(creator.text or ""))] = (creator, role_node)
        kept: set[str] = set()
        for position, name in enumerate(names, 1):
            pair = existing.get(_normal(name))
            if pair:
                creator, role_node = pair
                creator.text = name
            else:
                creator_id = _new_id(creators, role, position)
                creator = etree.Element(f"{{{NS['dc']}}}creator", id=creator_id)
                creator.text = name
                role_node = etree.Element(
                    f"{{{NS['opf']}}}meta",
                    refines=f"#{creator_id}",
                    property="role",
                    scheme="marc:relators",
                )
                role_node.text = role
                metadata.extend((creator, role_node))
                creators[creator_id] = creator
            kept.add(str(creator.get("id")))
        for creator, role_node in existing.values():
            creator_id = str(creator.get("id"))
            if creator_id in kept:
                continue
            metadata.remove(role_node)
            refs = metadata.xpath("opf:meta[@refines=$ref]", namespaces=NS, ref=f"#{creator_id}")
            if not refs:
                metadata.remove(creator)


def _verify_fields(
    metadata: etree._Element,
    fields: Mapping[str, object],
    exact_date: str | None,
    roles: Mapping[str, Sequence[str]],
    errors: list[str],
) -> None:
    simple = {
        "title": "title",
        "publisher": "publisher",
        "language": "language",
        "description": "description",
    }
    for field, element in simple.items():
        if field in fields:
            actual = metadata.xpath(f"string(dc:{element}[1])", namespaces=NS)
            if actual != str(fields[field]):
                errors.append(f"{field} read-back mismatch")
    authors = fields.get("authors")
    if authors is not None:
        if not isinstance(authors, Sequence) or isinstance(authors, str):
            errors.append("authors expectation is invalid")
        else:
            actual_authors = _creators_for_role(metadata, "aut")
            if actual_authors != list(authors):
                errors.append("authors read-back mismatch")
    subjects = fields.get("subjects")
    if subjects is not None:
        if not isinstance(subjects, Sequence) or isinstance(subjects, str):
            errors.append("subjects expectation is invalid")
        elif metadata.xpath("dc:subject/text()", namespaces=NS) != list(subjects):
            errors.append("subjects read-back mismatch")
    identifiers = fields.get("identifiers")
    if identifiers is not None:
        actual_identifiers = [
            str(value) for value in metadata.xpath("dc:identifier/text()", namespaces=NS)
        ]
        if not isinstance(identifiers, Mapping):
            errors.append("identifiers expectation is invalid")
        elif any(
            not any(str(value) in actual for actual in actual_identifiers)
            for value in identifiers.values()
        ):
            errors.append("identifiers read-back mismatch")
    if "series" in fields or "series_index" in fields:
        series, index = _read_series(metadata)
        if "series" in fields and series != str(fields["series"]):
            errors.append("series read-back mismatch")
        if "series_index" in fields and index != str(fields["series_index"]):
            errors.append("series index read-back mismatch")
    if exact_date is not None:
        actual_date = metadata.xpath("string(dc:date[1])", namespaces=NS)
        if actual_date != exact_date:
            errors.append("exact publication date read-back mismatch")
    creators = {node.get("id"): str(node.text or "") for node in metadata.findall("dc:creator", NS)}
    for role, expected in roles.items():
        actual = []
        for node in metadata.findall("opf:meta", NS):
            if node.get("property") == "role" and str(node.text or "") == role:
                actual.append(creators.get(node.get("refines", "")[1:], ""))
        if actual != list(expected):
            errors.append(f"contributor role {role} read-back mismatch")


def _resource_hashes(epub: Path) -> dict[str, str]:
    _, opf_path = _read_opf(epub)
    with zipfile.ZipFile(epub) as archive:
        return {
            info.filename: hashlib.sha256(archive.read(info.filename)).hexdigest()
            for info in archive.infolist()
            if info.filename != opf_path
        }


def _opf_structure(epub: Path) -> tuple[object, ...]:
    root, _ = _read_opf(epub)
    manifest = tuple(
        sorted(
            (
                str(node.get("id")),
                str(node.get("href")),
                str(node.get("media-type")),
                str(node.get("properties") or ""),
            )
            for node in root.findall("opf:manifest/opf:item", NS)
        )
    )
    spine = tuple(root.xpath("opf:spine/opf:itemref/@idref", namespaces=NS))
    cover = str(
        root.xpath("string(opf:metadata/opf:meta[@name='cover'][1]/@content)", namespaces=NS)
    )
    unique_id = str(root.get("unique-identifier") or "")
    package_id = str(
        root.xpath(
            "string(opf:metadata/dc:identifier[@id=$identifier][1])",
            namespaces=NS,
            identifier=unique_id,
        )
    )
    return unique_id, package_id, manifest, spine, cover


def _unowned_metadata(epub: Path, owned_roles: set[str]) -> tuple[tuple[str, ...], ...]:
    root, _ = _read_opf(epub)
    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        return ()
    output: list[tuple[str, ...]] = []
    creators = {node.get("id"): str(node.text or "") for node in metadata.findall("dc:creator", NS)}
    for node in metadata.findall("opf:meta", NS):
        if node.get("property") == "role":
            role = str(node.text or "")
            if role in owned_roles or role == "aut":
                continue
            output.append(("role", role, creators.get(node.get("refines", "")[1:], "")))
        elif node.get("property") and node.get("property") not in {
            "belongs-to-collection",
            "collection-type",
            "group-position",
        }:
            output.append(("property", str(node.get("property")), str(node.text or "")))
    return tuple(sorted(output))


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _creators_for_role(metadata: etree._Element, role: str) -> list[str]:
    output: list[str] = []
    for creator in metadata.findall("dc:creator", NS):
        creator_id = creator.get("id")
        resolved = creator.get(f"{{{NS['opf']}}}role")
        if not resolved and creator_id:
            resolved = str(
                metadata.xpath(
                    "string(opf:meta[@refines=$ref and @property='role'][1])",
                    namespaces=NS,
                    ref=f"#{creator_id}",
                )
            )
        if (resolved or "aut") == role:
            output.append(str(creator.text or ""))
    return output


def _read_series(metadata: etree._Element) -> tuple[str, str]:
    series = str(
        metadata.xpath("string(opf:meta[@name='calibre:series'][1]/@content)", namespaces=NS)
    )
    index = str(
        metadata.xpath("string(opf:meta[@name='calibre:series_index'][1]/@content)", namespaces=NS)
    )
    for node in metadata.findall("opf:meta", NS):
        if node.get("property") != "belongs-to-collection" or not node.get("id"):
            continue
        ref = f"#{node.get('id')}"
        collection_type = metadata.xpath(
            "string(opf:meta[@refines=$ref and @property='collection-type'][1])",
            namespaces=NS,
            ref=ref,
        )
        if collection_type == "series":
            series = str(node.text or "")
            index = str(
                metadata.xpath(
                    "string(opf:meta[@refines=$ref and @property='group-position'][1])",
                    namespaces=NS,
                    ref=ref,
                )
            )
            break
    return series, index


def _new_id(creators: Mapping[str | None, etree._Element], role: str, position: int) -> str:
    base = f"kavita-ingest-{role}-{position}"
    value = base
    suffix = 1
    while value in creators:
        suffix += 1
        value = f"{base}-{suffix}"
    return value
