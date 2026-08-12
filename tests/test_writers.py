from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pikepdf
import pytest

from compatibility.helpers.epub_factory import create_epub, publication_hashes
from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.canonical import CanonicalIdentity
from kavita_ingest.comicinfo import ComicInfoError, patch_comicinfo, read_comicinfo
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.projection import project_comic
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


def test_comicinfo_normalizes_matching_legacy_issue_alias() -> None:
    source = b"""<ComicInfo>
      <Series>Absolute Green Lantern</Series>
      <Issue>004</Issue>
    </ComicInfo>"""

    output = patch_comicinfo(
        source,
        set_fields={
            "Series": "Absolute Green Lantern (2025)",
            "Number": "4",
        },
    )

    document = read_comicinfo(output, require_schema=True)

    assert document.metadata["Number"] == "4"
    assert b"<Issue>" not in output


def test_comicinfo_rejects_conflicting_legacy_issue_alias() -> None:
    source = b"""<ComicInfo>
      <Series>Absolute Green Lantern</Series>
      <Issue>5</Issue>
    </ComicInfo>"""

    with pytest.raises(
        ComicInfoError,
        match="conflicts with planned Number",
    ):
        patch_comicinfo(
            source,
            set_fields={
                "Series": "Absolute Green Lantern (2025)",
                "Number": "4",
            },
        )


def test_comicinfo_translator_is_owned_schema_ordered_and_read_back() -> None:
    output = patch_comicinfo(
        b"<ComicInfo><Publisher>Original</Publisher></ComicInfo>",
        set_fields={"Writer": "W. Writer", "Translator": "T. Translator"},
    )
    document = read_comicinfo(output, require_schema=True)
    assert document.metadata["Translator"] == "T. Translator"
    assert output.index(b"<Writer>") < output.index(b"<Translator>") < output.index(b"<Publisher>")


def test_comicinfo_reorders_existing_known_nodes_without_losing_pages() -> None:
    source = Path("tests/fixtures/comicinfo/out_of_order.xml").read_bytes()

    output = patch_comicinfo(
        source,
        set_fields={"Series": "Absolute Batman (2024)", "CoverArtist": "Peter Smith"},
    )

    document = read_comicinfo(output, require_schema=True)
    assert document.metadata["Series"] == "Absolute Batman (2024)"
    assert output.index(b"<Title>") < output.index(b"<Summary>") < output.index(b"<Notes>")
    assert output.index(b"<Notes>") < output.index(b"<PageCount>") < output.index(b"<Pages>")
    assert b'Bookmark="preserve-me"' in output
    assert output.count(b"<Page ") == 2


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("unknown_extension.xml", "ReadingOrder"),
        ("unknown_attribute.xml", "attribute 'vendor' is not allowed"),
        ("invalid_known_value.xml", "PageCount"),
    ],
)
def test_comicinfo_incompatibility_reports_exact_xsd_reason(
    fixture: str, reason: str
) -> None:
    source = Path("tests/fixtures/comicinfo") / fixture

    with pytest.raises(ComicInfoError) as captured:
        patch_comicinfo(source.read_bytes(), set_fields={"Series": "Absolute Batman (2024)"})

    message = str(captured.value)
    assert "schema validation failed" in message
    assert "failed: None" not in message
    assert reason in message
    assert "line " in message and "SCHEMASV/" in message


@pytest.mark.parametrize(
    ("fixture", "preserved"),
    [
        ("unknown_extension.xml", b"vendor:ReadingOrder"),
        ("unknown_attribute.xml", b'vendor="legacy-tool"'),
        ("invalid_known_value.xml", b"<PageCount>many</PageCount>"),
    ],
)
def test_non_strict_patch_preserves_incompatible_unowned_metadata(
    fixture: str, preserved: bytes
) -> None:
    source = (Path("tests/fixtures/comicinfo") / fixture).read_bytes()

    output = patch_comicinfo(
        source,
        set_fields={"Series": "Absolute Batman (2024)"},
        require_schema=False,
    )

    assert preserved in output
    assert read_comicinfo(output).schema_valid is False


def test_comicinfo_reader_uses_same_schema_for_diagnostics() -> None:
    source = Path("tests/fixtures/comicinfo/invalid_known_value.xml").read_bytes()

    with pytest.raises(ComicInfoError) as captured:
        read_comicinfo(source, require_schema=True)

    assert "failed: None" not in str(captured.value)
    assert "Manga" in str(captured.value) or "PageCount" in str(captured.value)


def test_comicinfo_legacy_root_has_specific_diagnostic() -> None:
    source = Path("tests/fixtures/comicinfo/legacy_structure.xml").read_bytes()

    with pytest.raises(ComicInfoError, match="ComicInfo root element is required"):
        patch_comicinfo(source, set_fields={"Series": "Absolute Batman (2024)"})


def test_namespaced_extension_matching_owned_local_name_is_not_overwritten() -> None:
    source = b"""<ComicInfo xmlns:vendor='https://example.invalid/vendor'>
      <vendor:Series vendor:source='legacy'>Extension series</vendor:Series>
    </ComicInfo>"""

    with pytest.raises(ComicInfoError, match="vendor:Series|Series"):
        patch_comicinfo(source, set_fields={"Series": "Owned series"})


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


def test_cbz_writer_verifies_trimmed_cover_artist_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "out.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")

    result = write_cbz_metadata(
        source,
        destination,
        set_fields={
            "Series": "Absolute Batman (2024)",
            "Number": "005",
            "CoverArtist": "Peter Smith",
        },
    )

    assert result.valid
    with zipfile.ZipFile(destination) as archive:
        metadata = read_comicinfo(archive.read("ComicInfo.xml"), require_schema=True).metadata
    assert metadata["CoverArtist"] == "Peter Smith"


@pytest.mark.parametrize("fixture", ["unknown_extension.xml", "invalid_known_value.xml"])
def test_incompatible_existing_comicinfo_keeps_source_and_publishes_nothing(
    fixture: str, tmp_path: Path
) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "out.cbz"
    comicinfo = (Path("tests/fixtures/comicinfo") / fixture).read_bytes()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("ComicInfo.xml", comicinfo)
    before = source.read_bytes()

    with pytest.raises(ComicInfoError, match="schema validation failed"):
        write_cbz_metadata(
            source,
            destination,
            set_fields={"Series": "Absolute Batman (2024)", "Number": "017"},
        )

    assert source.read_bytes() == before
    assert not destination.exists()


@pytest.mark.parametrize(
    ("release_date", "release_precision", "cover_date", "cover_precision"),
    [
        (None, None, None, None),
        ("2026", "year", None, None),
        ("2026-01", "month", None, None),
        ("2026-02-30", "day", None, None),
        (None, None, "2026-01", "month"),
    ],
)
def test_cbz_writer_preserves_existing_release_date_when_projection_is_not_exact(
    tmp_path: Path,
    release_date: str | None,
    release_precision: str | None,
    cover_date: str | None,
    cover_precision: str | None,
) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "out.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr(
            "ComicInfo.xml",
            b"<ComicInfo><Year>2020</Year><Month>5</Month><Day>7</Day></ComicInfo>",
        )

    projection = project_comic(
        CanonicalIdentity(
            MediaKind.COMIC,
            "Issue",
            (),
            series_title="Series",
            sequence=SequenceNumber.parse("1"),
            run_start_year=2024,
            item_type="issue",
            release_date=release_date,
            release_date_precision=release_precision,
            cover_date=cover_date,
            cover_date_precision=cover_precision,
        )
    )
    result = write_cbz_metadata(
        source,
        destination,
        set_fields=projection.ownership.set_fields,
        clear_fields=projection.ownership.clear_fields,
    )

    assert result.valid
    with zipfile.ZipFile(destination) as archive:
        metadata = read_comicinfo(archive.read("ComicInfo.xml"), require_schema=True).metadata
    assert (metadata["Year"], metadata["Month"], metadata["Day"]) == ("2020", "5", "7")


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
