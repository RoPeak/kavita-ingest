from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from lxml import etree

from .epub_factory import NS, OPF_PATH

OWNABLE_ROLES = frozenset({"trl", "edt", "ill", "clr"})


def _normal_name(value: str) -> str:
    return " ".join(value.casefold().split())


def patch_contributors(epub: Path, updates: Mapping[str, Sequence[str]]) -> None:
    invalid = set(updates) - OWNABLE_ROLES
    if invalid:
        raise ValueError(f"unsupported owned contributor roles: {sorted(invalid)}")

    with zipfile.ZipFile(epub) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}

    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    root = etree.fromstring(payloads[OPF_PATH], parser=parser)
    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError("EPUB OPF metadata element missing")

    creator_by_id = {
        node.get("id"): node
        for node in metadata.findall("dc:creator", NS)
        if node.get("id")
    }
    role_nodes: dict[str, list[etree._Element]] = {}
    for node in metadata.findall("opf:meta", NS):
        if node.get("property") == "role" and node.get("refines", "").startswith("#"):
            role_nodes.setdefault(str(node.text or ""), []).append(node)

    for role, desired_names in updates.items():
        existing: dict[str, tuple[etree._Element, etree._Element]] = {}
        for role_node in role_nodes.get(role, []):
            creator = creator_by_id.get(role_node.get("refines", "")[1:])
            if creator is not None:
                existing[_normal_name(str(creator.text or ""))] = (creator, role_node)

        keep_ids: set[str] = set()
        for position, name in enumerate(desired_names, start=1):
            existing_pair = existing.get(_normal_name(name))
            if existing_pair:
                creator, role_node = existing_pair
                creator.text = name
                keep_ids.add(str(creator.get("id")))
                role_node.text = role
                continue
            base_id = f"kavita-ingest-{role}-{position}"
            creator_id = base_id
            suffix = 1
            while creator_id in creator_by_id:
                suffix += 1
                creator_id = f"{base_id}-{suffix}"
            creator = etree.Element(f"{{{NS['dc']}}}creator", id=creator_id)
            creator.text = name
            role_node = etree.Element(
                f"{{{NS['opf']}}}meta",
                refines=f"#{creator_id}",
                property="role",
                scheme="marc:relators",
            )
            role_node.text = role
            metadata.append(creator)
            metadata.append(role_node)
            creator_by_id[creator_id] = creator
            keep_ids.add(creator_id)

        for creator, role_node in existing.values():
            creator_id = str(creator.get("id"))
            if creator_id in keep_ids:
                continue
            metadata.remove(role_node)
            other_refinements = metadata.xpath(
                "opf:meta[@refines=$ref]", namespaces=NS, ref=f"#{creator_id}"
            )
            if not other_refinements:
                metadata.remove(creator)

    payloads[OPF_PATH] = etree.tostring(root, encoding="utf-8", xml_declaration=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{epub.name}.", suffix=".tmp", dir=epub.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with zipfile.ZipFile(temp, "w") as target:
            for info in infos:
                target.writestr(info, payloads[info.filename])
        os.replace(temp, epub)
    finally:
        temp.unlink(missing_ok=True)
