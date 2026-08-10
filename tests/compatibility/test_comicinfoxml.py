from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path

from lxml import etree

from compatibility.helpers.archive_checks import patch_comicinfo

FIXTURE = Path("compatibility/fixtures/comicinfo/ComicInfo.xml")
SCHEMA = Path("compatibility/fixtures/comicinfo/ComicInfo-2.1.xsd")


def test_comicinfoxml_distribution_does_not_declare_its_runtime_comicapi_dependency() -> None:
    requirements = importlib.metadata.requires("comicinfoxml") or []
    assert not any(requirement.casefold().startswith("comicapi") for requirement in requirements)
    result = subprocess.run(
        [sys.executable, "-c", "import comicinfoxml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "comicapi" in result.stderr or "FileHash" in result.stderr


def test_lxml_alternative_modifies_owned_fields_and_preserves_unowned_fields() -> None:
    source = FIXTURE.read_bytes()
    output = patch_comicinfo(
        source,
        {
            "Series": "Absolute Batman (2024)",
            "Number": "70.5",
            "Volume": None,
            "Format": "Annual",
            "Title": "Projected Title",
            "Year": 2026,
            "Month": 2,
            "Day": 18,
            "Writer": "New Writer",
            "Editor": "New Editor",
            "Translator": "New Translator",
            "Publisher": "Projected Publisher",
            "LanguageISO": "en-US",
            "GTIN": "9781111111113",
        },
    )
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(output, parser=parser)

    assert root.findtext("Series") == "Absolute Batman (2024)"
    assert root.findtext("Number") == "70.5"
    assert root.find("Volume") is None
    assert root.findtext("Format") == "Annual"
    assert root.findtext("Title") == "Projected Title"
    assert root.findtext("Year") == "2026"
    assert root.findtext("Writer") == "New Writer"
    assert root.findtext("Publisher") == "Projected Publisher"
    assert root.findtext("LanguageISO") == "en-US"
    assert root.findtext("GTIN") == "9781111111113"

    assert root.findtext("Summary") == "Preserve this summary"
    assert root.findtext("Notes") == "Preserve these notes"
    assert root.findtext("StoryArc") == "Preserved Arc"
    assert root.findtext("SeriesGroup") == "Preserved Group"
    extension = root.find("FixtureExtension")
    assert extension is not None
    assert extension.get("fixture") == "true"
    assert extension.text == "preserve unknown extension"


def test_lxml_alternative_serializes_schema_valid_comicinfo_2_1() -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(FIXTURE.read_bytes(), parser=parser)
    root.remove(root.find("FixtureExtension"))
    output = patch_comicinfo(
        etree.tostring(root),
        {"Series": "Series (2024)", "Number": "1A", "Volume": None, "Format": "Special"},
    )
    schema = etree.XMLSchema(etree.parse(str(SCHEMA), parser=parser))
    schema.assertValid(etree.fromstring(output, parser=parser))
