from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from compatibility.helpers.epub_factory import (
    copy_epub,
    create_epub,
    opf_snapshot,
    publication_hashes,
    validate_epub,
)


def run_ebook_meta(epub: Path, arguments: list[str], config_dir: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("ebook-meta")
    if executable is None:
        pytest.skip("Calibre ebook-meta is not installed")
    environment = os.environ.copy()
    environment["CALIBRE_CONFIG_DIRECTORY"] = str(config_dir)
    return subprocess.run(
        [executable, str(epub), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


@pytest.fixture()
def base_epub(tmp_path: Path) -> Path:
    return create_epub(tmp_path / "base.epub")


@pytest.mark.parametrize(
    ("name", "arguments", "field", "expected"),
    [
        ("title", ["--title", "Changed Title"], "title", "Changed Title"),
        ("authors", ["--authors", "Casey Author"], "author", "Casey Author"),
        ("publisher", ["--publisher", "Changed Press"], "publisher", "Changed Press"),
        ("language", ["--language", "fr"], "language", "fr"),
        ("isbn", ["--isbn", "9781111111113"], "isbn", "9781111111113"),
        (
            "identifier",
            ["--identifier", "google:fixture-volume-id"],
            "identifier",
            "google:fixture-volume-id",
        ),
        ("description", ["--comments", "Changed description"], "description", "Changed description"),
        ("subjects", ["--tags", "One, Two"], "subjects", ["One", "Two"]),
        (
            "series",
            ["--series", "Changed Series", "--index", "2.5"],
            "series_index",
            ("Changed Series", "2.5"),
        ),
    ],
)
def test_ebook_meta_field_capabilities(
    base_epub: Path,
    tmp_path: Path,
    name: str,
    arguments: list[str],
    field: str,
    expected: object,
) -> None:
    target = copy_epub(base_epub, tmp_path / f"{name}.epub")
    before_hashes = publication_hashes(target)
    result = run_ebook_meta(target, arguments, tmp_path / "calibre-config")
    assert result.returncode == 0, result.stderr
    validate_epub(target)
    after = opf_snapshot(target)
    assert publication_hashes(target) == before_hashes

    if field == "author":
        assert expected in after["creators"].values()
    elif field == "isbn":
        assert any(expected in value for value in after["identifiers"])
    elif field == "identifier":
        scheme, value = str(expected).split(":", 1)
        assert any(value in identifier for identifier in after["identifiers"]), (scheme, after)
    elif field == "subjects":
        assert set(after["subjects"]) == set(expected)
    elif field == "series_index":
        series, index = expected
        assert after["series"] == series
        assert after["series_index"] == index
    else:
        assert after[field] == expected


def test_ebook_meta_preservation_of_existing_contributor_role_refinements(
    base_epub: Path, tmp_path: Path
) -> None:
    target = copy_epub(base_epub, tmp_path / "roles.epub")
    before = opf_snapshot(target)
    result = run_ebook_meta(target, ["--title", "Only Title Changed"], tmp_path / "config")
    assert result.returncode == 0, result.stderr
    after = opf_snapshot(target)
    assert after["creators"] == before["creators"]


def test_ebook_meta_author_update_preserves_non_author_contributor_roles(
    base_epub: Path, tmp_path: Path
) -> None:
    target = copy_epub(base_epub, tmp_path / "authors-and-roles.epub")
    result = run_ebook_meta(target, ["--authors", "Casey Author"], tmp_path / "config")
    assert result.returncode == 0, result.stderr
    after = opf_snapshot(target)
    assert after["creators"] == {
        "aut": "Casey Author",
        "trl": "Terry Translator",
        "edt": "Eddie Editor",
        "ill": "Indigo Illustrator",
    }


def test_ebook_meta_date_is_timezone_shifted_and_requires_native_writing(
    base_epub: Path, tmp_path: Path
) -> None:
    target = copy_epub(base_epub, tmp_path / "date.epub")
    result = run_ebook_meta(target, ["--date", "2025-06-07"], tmp_path / "config")
    assert result.returncode == 0, result.stderr
    written = str(opf_snapshot(target)["date"])
    assert written != "2025-06-07"
    assert written.startswith("2025-06-06T23:00:00")


def test_ebook_meta_preserves_package_structure_and_custom_metadata(
    base_epub: Path, tmp_path: Path
) -> None:
    target = copy_epub(base_epub, tmp_path / "structure.epub")
    before = opf_snapshot(target)
    result = run_ebook_meta(target, ["--publisher", "New Publisher"], tmp_path / "config")
    assert result.returncode == 0, result.stderr
    after = opf_snapshot(target)
    assert after["unique_identifier"] == before["unique_identifier"]
    assert after["manifest_items"] == before["manifest_items"]
    assert after["spine_ids"] == before["spine_ids"]
    assert after["cover"] == before["cover"]
    assert after["custom_meta"] == before["custom_meta"]
