from __future__ import annotations

import zipfile
from pathlib import Path

from compatibility.helpers.epub_factory import CONTAINER, MIMETYPE, OPF, RESOURCES, create_epub
from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.archive_safety import ArchiveLimits
from kavita_ingest.domain import InspectionStatus, SourceFormat
from kavita_ingest.inspectors import inspect
from kavita_ingest.inspectors.comic import inspect_cbr

LIMITS = ArchiveLimits()


def test_epub_inspector_reads_legacy_series_and_fractional_index(tmp_path: Path) -> None:
    path = create_epub(tmp_path / "book.epub")
    result = inspect(path, SourceFormat.EPUB, LIMITS)
    assert result.status is InspectionStatus.OK
    assert result.metadata["title"] == "Fixture Book"
    assert result.metadata["series"] == "Fixture Series"
    assert result.metadata["series_index"] == "1.5"
    assert result.metadata["creators"] == ["Alex Author"]
    assert result.metadata["contributors"] == {
        "aut": ["Alex Author"],
        "trl": ["Terry Translator"],
        "edt": ["Eddie Editor"],
        "ill": ["Indigo Illustrator"],
    }


def test_epub_inspector_reads_epub3_series_and_fractional_index(tmp_path: Path) -> None:
    package = OPF.replace(
        b'<meta name="calibre:series" content="Fixture Series"/>',
        b'<meta id="series-id" property="belongs-to-collection">EPUB3 Series</meta>'
        b'<meta refines="#series-id" property="collection-type">series</meta>',
    ).replace(
        b'<meta name="calibre:series_index" content="1.5"/>',
        b'<meta refines="#series-id" property="group-position">2.5</meta>',
    )
    path = tmp_path / "epub3.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/package.opf", package)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
    result = inspect(path, SourceFormat.EPUB, LIMITS)
    assert result.metadata["series"] == "EPUB3 Series"
    assert result.metadata["series_index"] == "2.5"


def test_cbz_inspector_reads_comicinfo_and_counts_pages(tmp_path: Path) -> None:
    path = tmp_path / "comic.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"image")
        archive.writestr(
            "ComicInfo.xml",
            b"<ComicInfo><Title>Origin</Title><Series>Example</Series><Number>1A</Number></ComicInfo>",
        )
    result = inspect(path, SourceFormat.CBZ, LIMITS)
    assert result.status is InspectionStatus.OK
    assert result.metadata["page_count"] == 1
    assert result.metadata["comicinfo"] == {"Title": "Origin", "Series": "Example", "Number": "1A"}


def test_cbz_rejects_case_colliding_paths(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Page.jpg", b"one")
        archive.writestr("page.jpg", b"two")
    result = inspect(path, SourceFormat.CBZ, LIMITS)
    assert result.status is InspectionStatus.FAILED
    assert "case-colliding" in str(result.error_message)


def test_cbz_rejects_duplicate_owned_comicinfo_fields(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"image")
        archive.writestr(
            "ComicInfo.xml",
            b"<ComicInfo><Series>First</Series><Series>Second</Series></ComicInfo>",
        )
    result = inspect(path, SourceFormat.CBZ, LIMITS)
    assert result.status is InspectionStatus.FAILED
    assert "ambiguous duplicate" in str(result.error_message)


def test_multivolume_cbr_is_blocked_before_opening(tmp_path: Path) -> None:
    path = tmp_path / "comic.part01.rar"
    path.write_bytes(b"not even a rar")
    result = inspect_cbr(path, LIMITS)
    assert result.status is InspectionStatus.BLOCKED
    assert result.error_code == "multivolume_rar_unsupported"
    assert "one physical CBR/RAR file" in str(result.error_message)


def test_old_style_multivolume_cbr_is_blocked_from_first_file(tmp_path: Path) -> None:
    path = tmp_path / "comic.rar"
    path.write_bytes(b"not even a rar")
    (tmp_path / "comic.r00").write_bytes(b"companion")
    result = inspect_cbr(path, LIMITS)
    assert result.status is InspectionStatus.BLOCKED
    assert result.error_code == "multivolume_rar_unsupported"


def test_ordinary_rar3_and_rar5_are_inspected_read_only() -> None:
    fixtures = Path("compatibility/fixtures/rar")
    for filename in ("rar3-subdirs.rar", "rar5-subdirs.rar"):
        path = fixtures / filename
        before = path.read_bytes()
        result = inspect_cbr(path, LIMITS)
        assert result.status is InspectionStatus.OK
        assert path.read_bytes() == before


def test_pdf_inspector_reads_semantic_fingerprints_and_signature_marker(tmp_path: Path) -> None:
    ordinary = create_pdf(tmp_path / "ordinary.pdf")
    signed = create_pdf(tmp_path / "signed.pdf", signed_marker=True)
    result = inspect(ordinary, SourceFormat.PDF, LIMITS)
    signed_result = inspect(signed, SourceFormat.PDF, LIMITS)
    assert result.status is InspectionStatus.OK
    assert result.metadata["page_count"] == 2
    assert len(result.metadata["content_stream_sha256"]) == 2
    assert signed_result.metadata["signature_fields"] is True
    assert signed_result.warnings


def test_encrypted_pdf_is_a_blocked_durable_result(tmp_path: Path) -> None:
    path = create_pdf(tmp_path / "encrypted.pdf", encrypted=True)
    result = inspect(path, SourceFormat.PDF, LIMITS)
    assert result.status is InspectionStatus.BLOCKED
    assert result.error_code == "encrypted_pdf"
