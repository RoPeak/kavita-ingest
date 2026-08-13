from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from compatibility.helpers.epub_factory import create_epub
from compatibility.helpers.pdf_factory import create_pdf
from kavita_ingest.archive_safety import ArchiveLimits
from kavita_ingest.calibre import require_safe_calibre_executable
from kavita_ingest.canonical import CanonicalIdentity, ResolutionLevel, work_only_identity
from kavita_ingest.config import AppConfig
from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.filesystem import sha256_file
from kavita_ingest.naming import NamingPolicy
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.planning import (
    PlanningPolicySnapshot,
    SourcePrecondition,
    build_snapshot,
    new_plan,
)
from kavita_ingest.projection import project_book, project_comic


@dataclass(frozen=True, slots=True)
class ApplyFixture:
    config: AppConfig
    plan_id: int
    source: Path
    destination: Path
    archive: Path | None


def make_apply_fixture(
    tmp_path: Path,
    media_format: str = "epub",
    *,
    lifecycle: str = "move_after_verify",
    work_only: bool = False,
    plan_name: str | None = None,
    approve: bool = True,
    pdf_state: str = "ordinary",
    cbr_fixture: str = "rar5-subdirs.rar",
    writer_versions: dict[str, str] | None = None,
    file_mode: int = 0o644,
    directory_mode: int = 0o755,
) -> ApplyFixture:
    name = plan_name or f"{media_format}-{lifecycle}"
    incoming = tmp_path / name / "incoming"
    books = tmp_path / name / "books"
    comics = tmp_path / name / "comics"
    archive_root = tmp_path / name / "archive"
    for directory in (incoming, books, comics, archive_root):
        directory.mkdir(parents=True)
    source = _source(incoming, media_format, pdf_state=pdf_state, cbr_fixture=cbr_fixture)
    root = books if media_format in {"epub", "pdf"} else comics
    if media_format in {"epub", "pdf"}:
        identity = (
            work_only_identity(title="Resolved Book", creators=("Alex Author",))
            if work_only
            else CanonicalIdentity(
                MediaKind.BOOK,
                "Resolved Book",
                ("Alex Author",),
                publisher="Resolved Press",
                publication_date="2025-06-07",
                language="en-GB",
                identifiers={"isbn": "9780000000002"},
                contributors={"translators": ("Terry Translator",)},
                description="Resolved description",
                subjects=("Testing",),
                resolution=ResolutionLevel.MANUAL,
            )
        )
        projection = project_book(identity, f".{media_format}")
    else:
        identity = CanonicalIdentity(
            MediaKind.COMIC,
            "At Midnight",
            ("Alan Moore",),
            series_title="Watchmen",
            sequence=SequenceNumber.parse("1"),
            run_start_year=1986,
            item_type="issue",
            resolution=ResolutionLevel.MANUAL,
        )
        projection = project_comic(identity, ".cbz")
    archive = archive_root / source.name if lifecycle == "archive_after_verify" else None
    policy = PlanningPolicySnapshot(
        NamingPolicy(),
        lifecycle,
        str(archive_root) if lifecycle == "archive_after_verify" else None,
        True,
        ArchiveLimits(),
        file_mode,
        directory_mode,
    )
    snapshot = build_snapshot(
        item_id="item-1",
        source=SourcePrecondition(
            str(source),
            sha256_file(source),
            source.stat().st_size,
            source.stat().st_mtime_ns,
            media_format,
        ),
        identity=identity,
        projection=projection,
        decision_provenance={
            "decision_type": "manual_identity" if not work_only else "work_accepted",
            "decision_id": 1,
            "explicit_approval": True,
        },
        transformations=({"type": _transformation(media_format)},),
        writer_versions=writer_versions or _versions(media_format),
        expected_inventory=(),
        verification_requirements=("source_precondition", "metadata_readback"),
        lifecycle_policy=lifecycle,
        archive_path=str(archive) if archive else None,
        destination_root=str(root),
        planning_policy=policy,
    )
    database = tmp_path / name / "state.sqlite3"
    config = AppConfig(
        database_path=database,
        books_root=books,
        comics_root=comics,
        staging_root=tmp_path / name / "unused-staging",
        published_file_mode=file_mode,
        created_directory_mode=directory_mode,
    )
    migrate(database)
    with connect(database) as connection:
        store = PlanStore(connection)
        plan = store.add(new_plan(name, (snapshot,), policy))
        if approve:
            store.approve(plan.id, plan.sha256)
    destination = root / projection.destination
    return ApplyFixture(config, plan.id, source, destination, archive)


def _source(
    incoming: Path,
    media_format: str,
    *,
    pdf_state: str,
    cbr_fixture: str,
) -> Path:
    if media_format == "epub":
        return create_epub(incoming / "source.epub")
    if media_format == "pdf":
        return create_pdf(
            incoming / "source.pdf",
            encrypted=pdf_state == "encrypted",
            signed_marker=pdf_state == "signed",
        )
    if media_format == "cbz":
        path = incoming / "source.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", b"first-page")
            archive.writestr("002.jpg", b"second-page")
        return path
    if media_format == "cbr":
        path = incoming / "source.cbr"
        shutil.copy2(Path("compatibility/fixtures/rar") / cbr_fixture, path)
        return path
    raise ValueError(media_format)


def _versions(media_format: str) -> dict[str, str]:
    if media_format == "epub":
        return {
            "ebook-meta": require_safe_calibre_executable(
                "ebook-meta"
            ),
            "opf_patcher": "1",
        }

    if media_format == "pdf":
        return {
            "ebook-meta": require_safe_calibre_executable(
                "ebook-meta"
            ),
            "pikepdf": "10.11.0",
        }
    if media_format == "cbz":
        return {"comicinfo_schema": "2.1"}
    return {"comicinfo_schema": "2.1", "rarfile": "4.5", "unrar": "7.00"}


def _transformation(media_format: str) -> str:
    return "cbr_to_cbz" if media_format == "cbr" else "metadata_only"
