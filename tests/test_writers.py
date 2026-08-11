from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pikepdf
import pytest

from compatibility.helpers.epub_factory import create_epub, publication_hashes
from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.comicinfo import ComicInfoError, patch_comicinfo, read_comicinfo
from kavita_ingest.writers.comic import write_cbz_metadata
from kavita_ingest.writers.epub import write_epub
from kavita_ingest.writers.pdf import write_pdf_metadata
from kavita_ingest.writers.repack import repack_cbr_to_cbz


def test_comicinfo_patcher_preserves_unowned_standard_fields_and_schema_order() -> None:
    source = b"""<?xml version='1.0'?><ComicInfo><Title>Old</Title>
      <Notes>unowned</Notes><PageCount>2</PageCount></ComicInfo>"""
    output = patch_comicinfo(
        source, set_fields={"Series": "Watchmen (1986)", "Number": "1A", "Format": "Special"}
    )
    document = read_comicinfo(output, require_schema=True)
    assert document.metadata["Series"] == "Watchmen (1986)"
    assert b"<Notes>unowned</Notes>" in output
    assert output.index(b"<Series>") < output.index(b"<Notes>")


def test_comicinfo_rejects_duplicate_owned_fields() -> None:
    with pytest.raises(ComicInfoError, match="duplicate"):
        patch_comicinfo(
            b"<ComicInfo><Series>A</Series><Series>B</Series></ComicInfo>",
            set_fields={"Series": "C"},
        )


def test_cbz_writer_preserves_page_bytes_and_reads_back_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "out.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.jpg", b"page-two")
    before = source.read_bytes()
    result = write_cbz_metadata(
        source,
        destination,
        set_fields={"Series": "Watchmen (1986)", "Number": "1", "Title": "At Midnight"},
    )
    assert result.valid and source.read_bytes() == before
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("001.jpg") == b"page-one"
        assert (
            read_comicinfo(archive.read("ComicInfo.xml"), require_schema=True).metadata["Number"]
            == "1"
        )


def test_native_epub_patch_preserves_resources_and_exact_roles(tmp_path: Path) -> None:
    source = create_epub(tmp_path / "source.epub")
    destination = tmp_path / "out.epub"
    before = source.read_bytes()
    resources = publication_hashes(source)
    result = write_epub(
        source,
        destination,
        calibre_fields={},
        exact_date="2025-06-07",
        contributor_roles={"trl": ("New Translator",), "ill": ("Indigo Illustrator",)},
    )
    assert result.valid and source.read_bytes() == before
    assert publication_hashes(destination) == resources


@pytest.mark.skipif(shutil.which("ebook-meta") is None, reason="Calibre ebook-meta unavailable")
def test_epub_writer_reads_back_every_calibre_owned_field(tmp_path: Path) -> None:
    source = create_epub(tmp_path / "source.epub")
    destination = tmp_path / "out.epub"
    result = write_epub(
        source,
        destination,
        calibre_fields={
            "title": "Resolved Title",
            "authors": ("First Author", "Second Author"),
            "publisher": "Resolved Press",
            "language": "en-GB",
            "identifiers": {"isbn": "9780000000002", "google": "volume-123"},
            "description": "Resolved description",
            "subjects": ("One", "Two"),
            "series": "Resolved Series",
            "series_index": "2.5",
        },
        exact_date="2025-06-07",
        contributor_roles={"trl": ("Resolved Translator",), "edt": ("Resolved Editor",)},
    )
    assert result.valid
    assert set(result.checks) == {
        "zip_structure",
        "publication_resource_hashes",
        "opf_structure",
        "unowned_metadata",
        "metadata_readback",
    }


def test_pdf_writer_preserves_semantic_page_payloads_and_blocks_unsafe_states(
    tmp_path: Path,
) -> None:
    source = create_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "out.pdf"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert write_pdf_metadata(
        source,
        destination,
        fields={"title": "New", "author": "Writer", "language": "en-GB", "date": "2024-01-02"},
    ).valid
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    encrypted = create_pdf(tmp_path / "encrypted.pdf", encrypted=True)
    with pytest.raises(pikepdf.PasswordError):
        write_pdf_metadata(encrypted, tmp_path / "encrypted-out.pdf", fields={"title": "No"})
    signed = create_pdf(tmp_path / "signed.pdf", signed_marker=True)
    with pytest.raises(ValueError, match="signature-bearing"):
        write_pdf_metadata(signed, tmp_path / "signed-out.pdf", fields={"title": "No"})


@pytest.mark.parametrize("fixture", ["rar3-subdirs.rar", "rar5-subdirs.rar"])
def test_cbr_repack_preserves_regular_payloads_and_adds_comicinfo(
    fixture: str, tmp_path: Path
) -> None:
    source = Path("compatibility/fixtures/rar") / fixture
    destination = tmp_path / f"{fixture}.cbz"
    result = repack_cbr_to_cbz(
        source, destination, set_fields={"Series": "Fixture (2024)", "Number": "1"}
    )
    assert result.valid
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert "ComicInfo.xml" in archive.namelist()


def test_cbr_repack_blocks_multivolume(tmp_path: Path) -> None:
    source = Path("compatibility/fixtures/rar/rar5-vols.part1.rar")
    with pytest.raises(ValueError, match="multi-volume"):
        repack_cbr_to_cbz(source, tmp_path / "out.cbz", set_fields={"Series": "A"})
