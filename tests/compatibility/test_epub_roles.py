from __future__ import annotations

from pathlib import Path

from compatibility.helpers.epub_factory import (
    create_epub,
    opf_snapshot,
    publication_hashes,
    validate_epub,
)
from compatibility.helpers.opf_patch import patch_contributors


def test_narrow_opf_patch_updates_only_owned_contributor_roles(tmp_path: Path) -> None:
    epub = create_epub(tmp_path / "roles.epub")
    before = opf_snapshot(epub)
    resource_hashes = publication_hashes(epub)

    patch_contributors(
        epub,
        {
            "trl": ["Taylor Translator"],
            "ill": ["Indigo Illustrator", "Iris Illustrator"],
        },
    )

    validate_epub(epub)
    after = opf_snapshot(epub)
    assert publication_hashes(epub) == resource_hashes
    assert after["creators"]["aut"] == "Alex Author"
    assert after["creators"]["edt"] == "Eddie Editor"
    assert after["creators"]["trl"] == "Taylor Translator"
    assert "Iris Illustrator" in after["creator_lists"]["ill"]
    assert after["unique_identifier"] == before["unique_identifier"]
    assert after["manifest"] == before["manifest"]
    assert after["spine"] == before["spine"]
    assert after["cover"] == before["cover"]
    assert after["custom_meta"] == "preserve-me"


def test_narrow_opf_patch_reuses_stable_ids_for_unchanged_contributors(tmp_path: Path) -> None:
    epub = create_epub(tmp_path / "stable.epub")
    patch_contributors(epub, {"trl": ["Terry Translator"]})
    first = opf_snapshot(epub)
    patch_contributors(epub, {"trl": ["Terry Translator"]})
    second = opf_snapshot(epub)
    assert second["creator_ids"] == first["creator_ids"]
