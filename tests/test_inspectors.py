from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compatibility.helpers.epub_factory import CONTAINER, MIMETYPE, OPF, RESOURCES, create_epub
from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.archive_safety import ArchiveLimits
from kavita_ingest.cli import app
from kavita_ingest.domain import InspectionStatus, SourceFormat
from kavita_ingest.epub_repair import repair_epub
from kavita_ingest.epub_validation import EpubDisposition, validate_epub_container
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


def _epub_with_mimetype_defect(path: Path, defect: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        if defect == "not-first":
            archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "mimetype",
            MIMETYPE,
            compress_type=(zipfile.ZIP_DEFLATED if defect == "compressed" else zipfile.ZIP_STORED),
        )
        if defect != "not-first":
            archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/package.opf", OPF)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
    return path


@pytest.mark.parametrize("defect", ["not-first", "compressed"])
def test_epub_packaging_defects_fail_scan_and_are_repairable(
    tmp_path: Path, defect: str
) -> None:
    path = _epub_with_mimetype_defect(tmp_path / f"{defect}.epub", defect)
    validation = validate_epub_container(path, LIMITS)
    result = inspect(path, SourceFormat.EPUB, LIMITS)
    assert validation.disposition is EpubDisposition.REPAIRABLE
    assert result.status is InspectionStatus.FAILED
    assert result.error_code == "invalid_epub_packaging"
    assert result.metadata["repairable"] is True


def test_epub_repair_writes_new_valid_copy_and_content_manifest(tmp_path: Path) -> None:
    source = _epub_with_mimetype_defect(tmp_path / "source.epub", "not-first")
    before = source.read_bytes()
    output = tmp_path / "repaired.epub"
    result = repair_epub(source, output, LIMITS, dry_run=False)
    assert result is not None
    assert source.read_bytes() == before
    assert validate_epub_container(output, LIMITS).valid
    assert Path(result.report).is_file()
    assert result.member_hashes == validate_epub_container(output, LIMITS).member_hashes
    with pytest.raises(FileExistsError):
        repair_epub(source, output, LIMITS, dry_run=False)


def test_epub_repair_cli_requires_consent_and_supports_dry_run(tmp_path: Path) -> None:
    source = _epub_with_mimetype_defect(tmp_path / "source.epub", "not-first")
    output = tmp_path / "repaired.epub"
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\nincoming = ["{tmp_path}"]\n', encoding="utf-8")
    runner = CliRunner()
    preview = runner.invoke(
        app,
        [
            "epub-repair",
            str(source),
            "--output",
            str(output),
            "--dry-run",
            "--config",
            str(config),
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert "source remains unchanged" in preview.output
    assert not output.exists()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    outside_preview = runner.invoke(
        app,
        [
            "epub-repair",
            str(source),
            "--output",
            str(outside / "repaired.epub"),
            "--dry-run",
            "--config",
            str(config),
        ],
    )
    assert outside_preview.exit_code == 2
    assert "must remain inside the source Incoming root" in outside_preview.output
    refused = runner.invoke(
        app,
        ["epub-repair", str(source), "--output", str(output), "--apply", "--config", str(config)],
    )
    assert refused.exit_code == 2
    applied = runner.invoke(
        app,
        [
            "epub-repair",
            str(source),
            "--output",
            str(output),
            "--apply",
            "--yes",
            "--config",
            str(config),
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert output.exists()


def test_epub_repair_refuses_source_and_archive_symlinks(tmp_path: Path) -> None:
    source = _epub_with_mimetype_defect(tmp_path / "source.epub", "not-first")
    source_link = tmp_path / "source-link.epub"
    source_link.symlink_to(source)
    with pytest.raises(ValueError, match="must not be a symlink"):
        repair_epub(source_link, tmp_path / "linked-repair.epub", LIMITS, dry_run=False)

    archive_link = tmp_path / "archive-link.epub"
    with zipfile.ZipFile(archive_link, "w") as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/package.opf", OPF)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
        link = zipfile.ZipInfo("OEBPS/link.xhtml")
        link.create_system = 3
        link.external_attr = (0o120777 << 16) | 0xA000
        archive.writestr(link, b"chapter.xhtml")
    validation = validate_epub_container(archive_link, LIMITS)
    assert validation.disposition is EpubDisposition.BROKEN
    assert "archive links are unsupported" in validation.findings[-1].message


def test_epub_preflight_classifies_zip_encryption_as_unsupported(tmp_path: Path) -> None:
    path = create_epub(tmp_path / "encrypted.epub")
    member_name = "OEBPS/chapter.xhtml"
    with zipfile.ZipFile(path) as archive:
        local_offset = archive.getinfo(member_name).header_offset
    payload = bytearray(path.read_bytes())
    local_flags = int.from_bytes(payload[local_offset + 6 : local_offset + 8], "little")
    payload[local_offset + 6 : local_offset + 8] = (local_flags | 1).to_bytes(2, "little")
    cursor = 0
    while True:
        cursor = payload.find(b"PK\x01\x02", cursor)
        assert cursor >= 0
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = bytes(payload[cursor + 46 : cursor + 46 + name_length]).decode()
        if name == member_name:
            central_flags = int.from_bytes(payload[cursor + 8 : cursor + 10], "little")
            payload[cursor + 8 : cursor + 10] = (central_flags | 1).to_bytes(2, "little")
            break
        cursor += 46 + name_length + extra_length + comment_length
    path.write_bytes(payload)

    validation = validate_epub_container(path, LIMITS)

    assert validation.disposition is EpubDisposition.UNSUPPORTED
    assert validation.findings[0].code == "encrypted_archive_member"


@pytest.mark.parametrize("protected", ["META-INF/encryption.xml", "META-INF/signatures.xml"])
def test_epub_repair_refuses_encryption_and_signatures(tmp_path: Path, protected: str) -> None:
    source = _epub_with_mimetype_defect(tmp_path / "protected.epub", "not-first")
    rebuilt = tmp_path / "with-protection.epub"
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(rebuilt, "w") as target:
        for info in original.infolist():
            target.writestr(info, original.read(info))
        target.writestr(protected, b"<container/>")
    validation = validate_epub_container(rebuilt, LIMITS)
    assert validation.disposition is EpubDisposition.UNSUPPORTED
    before = rebuilt.read_bytes()
    refused = tmp_path / "refused.epub"
    with pytest.raises(ValueError, match="repair is refused"):
        repair_epub(rebuilt, refused, LIMITS, dry_run=False)
    assert rebuilt.read_bytes() == before
    assert not refused.exists()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing-mimetype", "mimetype_count"),
        ("invalid-mimetype", "mimetype_value"),
        ("duplicate", "invalid_epub"),
        ("unsafe-path", "invalid_epub"),
        ("missing-package", "invalid_epub"),
    ],
)
def test_epub_preflight_rejects_broken_or_unsafe_containers(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    path = tmp_path / f"{mutation}.epub"
    with zipfile.ZipFile(path, "w") as archive:
        if mutation != "missing-mimetype":
            archive.writestr(
                "mimetype",
                b"wrong" if mutation == "invalid-mimetype" else MIMETYPE,
                compress_type=zipfile.ZIP_STORED,
            )
        archive.writestr("META-INF/container.xml", CONTAINER)
        if mutation != "missing-package":
            archive.writestr("OEBPS/package.opf", OPF)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
        if mutation == "duplicate":
            archive.writestr("OEBPS/chapter.xhtml", b"duplicate")
        if mutation == "unsafe-path":
            archive.writestr("../escape.txt", b"unsafe")
    validation = validate_epub_container(path, LIMITS)
    assert validation.disposition is EpubDisposition.BROKEN
    assert expected in {item.code for item in validation.findings}


def test_epub_preflight_rejects_malformed_central_directory_and_bad_crc(
    tmp_path: Path,
) -> None:
    malformed = create_epub(tmp_path / "malformed.epub")
    malformed.write_bytes(malformed.read_bytes()[:-20])
    assert validate_epub_container(malformed, LIMITS).disposition is EpubDisposition.BROKEN

    corrupt = create_epub(tmp_path / "corrupt.epub")
    payload = corrupt.read_bytes()
    marker = RESOURCES["OEBPS/cover.png"]
    offset = payload.index(marker)
    damaged = bytearray(payload)
    damaged[offset + len(marker) - 1] ^= 1
    corrupt.write_bytes(damaged)
    validation = validate_epub_container(corrupt, LIMITS)
    assert validation.disposition is EpubDisposition.BROKEN
    assert "CRC" in validation.findings[-1].message


def test_epub_preflight_enforces_archive_resource_limits(tmp_path: Path) -> None:
    path = create_epub(tmp_path / "limited.epub")
    validation = validate_epub_container(path, ArchiveLimits(max_entries=2))
    assert validation.disposition is EpubDisposition.BROKEN
    assert "limit" in validation.findings[-1].message


def test_epub_with_multiple_renditions_requires_review(tmp_path: Path) -> None:
    multiple = CONTAINER.replace(
        b"</rootfiles>",
        b'<rootfile full-path="OEBPS/alternate.opf" '
        b'media-type="application/oebps-package+xml"/></rootfiles>',
    )
    path = tmp_path / "renditions.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", multiple)
        archive.writestr("OEBPS/package.opf", OPF)
        archive.writestr("OEBPS/alternate.opf", OPF)
        for name, payload in RESOURCES.items():
            archive.writestr(name, payload)
    validation = validate_epub_container(path, LIMITS)
    result = inspect(path, SourceFormat.EPUB, LIMITS)
    assert validation.disposition is EpubDisposition.UNCERTAIN
    assert result.status is InspectionStatus.BLOCKED
    assert result.error_code == "epub_review_required"


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
