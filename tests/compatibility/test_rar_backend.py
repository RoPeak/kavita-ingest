from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import rarfile

from compatibility.helpers.archive_checks import (
    ArchiveLimits,
    safe_extract_regular_files,
    validate_inventory,
)

FIXTURES = Path("compatibility/fixtures/rar")


def test_unrar_backend_is_detected() -> None:
    setup = rarfile.tool_setup()
    assert setup.setup["open_cmd"][0] == "UNRAR_TOOL"


@pytest.mark.parametrize("filename", ["rar3-subdirs.rar", "rar5-subdirs.rar"])
def test_rar3_and_rar5_inventory_order_and_payload_preservation(
    filename: str, tmp_path: Path
) -> None:
    with rarfile.RarFile(FIXTURES / filename) as archive:
        infos = archive.infolist()
        expected_order = [info.filename for info in infos]
        source_hashes = {
            info.filename.replace("\\", "/"): hashlib.sha256(archive.read(info)).hexdigest()
            for info in infos
            if not info.is_dir() and not info.is_symlink()
        }
        extracted_hashes = safe_extract_regular_files(archive, tmp_path / "out")
    assert list(extracted_hashes) == [
        name.replace("\\", "/")
        for name in expected_order
        if name.replace("\\", "/") in extracted_hashes
    ]
    assert extracted_hashes == source_hashes


@pytest.mark.parametrize(
    "filename", ["rar5-symlink-unix.rar", "rar5-evil-symlink-traversal.rar"]
)
def test_safe_extractor_rejects_symlinks_before_writing(filename: str, tmp_path: Path) -> None:
    destination = tmp_path / "out"
    with rarfile.RarFile(FIXTURES / filename) as archive:
        with pytest.raises(ValueError, match="links are not supported"):
            safe_extract_regular_files(archive, destination)
    assert not destination.exists()


def test_duplicate_entries_are_exposed_and_rejected() -> None:
    with rarfile.RarFile(FIXTURES / "rar5-dups.rar") as archive:
        names = [info.filename for info in archive.infolist()]
        assert len(names) == len(set(names))
        assert validate_inventory(archive.infolist()) == names

    duplicate_infos = [
        SimpleNamespace(filename="page.jpg", file_size=10, compress_size=10, is_symlink=lambda: False),
        SimpleNamespace(filename="page.jpg", file_size=10, compress_size=10, is_symlink=lambda: False),
    ]
    with pytest.raises(ValueError, match="duplicate or case-colliding"):
        validate_inventory(duplicate_infos)


def test_case_collisions_and_unsafe_paths_are_rejected_without_extraction() -> None:
    infos = [
        SimpleNamespace(filename="Pages/001.jpg", file_size=10, compress_size=10, is_symlink=lambda: False),
        SimpleNamespace(filename="pages/001.JPG", file_size=10, compress_size=10, is_symlink=lambda: False),
    ]
    with pytest.raises(ValueError, match="case-colliding"):
        validate_inventory(infos)
    infos[1].filename = "../outside.txt"
    with pytest.raises(ValueError, match="unsafe archive path"):
        validate_inventory(infos)


def test_archive_limits_are_checked_before_extraction() -> None:
    info = SimpleNamespace(
        filename="page.jpg",
        file_size=101,
        compress_size=1,
        is_symlink=lambda: False,
    )
    with pytest.raises(ValueError, match="entry-size"):
        validate_inventory([info], ArchiveLimits(max_entry_size=100))
    with pytest.raises(ValueError, match="compression-ratio"):
        validate_inventory([info], ArchiveLimits(max_entry_size=1_000, max_compression_ratio=10))


def test_header_encrypted_archive_is_reported_without_password() -> None:
    with rarfile.RarFile(FIXTURES / "rar5-hpsw.rar") as archive:
        assert archive.needs_password()
        assert archive.infolist() == []


def test_incomplete_old_style_multivolume_archive_is_not_safely_readable() -> None:
    with rarfile.RarFile(FIXTURES / "rar3-old.rar") as archive:
        infos = archive.infolist()
        assert infos
        first_file = next(info for info in infos if not info.is_dir())
        with pytest.raises(FileNotFoundError, match=r"rar3-old\.r00"):
            archive.read(first_file)


@pytest.mark.parametrize("filename", ["rar3-vols.part1.rar", "rar5-vols.part1.rar"])
def test_complete_multivolume_sets_can_be_read(filename: str) -> None:
    with rarfile.RarFile(FIXTURES / filename) as archive:
        infos = archive.infolist()
        assert infos
        for info in infos:
            if not info.is_dir():
                assert archive.read(info)


def test_non_rar_payload_is_reported_as_unsupported(tmp_path: Path) -> None:
    payload = tmp_path / "not-a-rar.cbr"
    payload.write_bytes(b"not a rar archive")
    with pytest.raises(rarfile.NotRarFile):
        rarfile.RarFile(payload)
