from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import InspectionStatus, SourceFormat
from .inspectors import inspect
from .locking import ProcessLock
from .paths import AppPaths
from .writers.epub import _opf_structure, _resource_hashes

PLAN_SCHEMA = "kavita-publication-reconciliation-v1"
STATE_SCHEMA = "kavita-reconciliation-state-v1"
APPROVAL_SCHEMA = "kavita-reconciliation-approval-v1"
JOURNAL_SCHEMA = "kavita-reconciliation-journal-v1"
REPORT_SCHEMA = "kavita-ingest-supersession-v1"


class ReconciliationRefused(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inspect_publication(path: Path, config: AppConfig) -> dict[str, Any]:
    resolved = _regular(path)
    result = inspect(resolved, SourceFormat.EPUB, config.archive_limits())
    resources: dict[str, str] = {}
    structure: tuple[object, ...] = ()
    if result.status is InspectionStatus.OK:
        resources = _resource_hashes(resolved)
        structure = _opf_structure(resolved)
    info = resolved.stat()
    metadata = result.metadata
    return {
        "path": str(resolved),
        "size": info.st_size,
        "sha256": sha256_file(resolved),
        "device": info.st_dev,
        "inode": info.st_ino,
        "links": info.st_nlink,
        "epub_status": result.status.value,
        "error": result.error_message,
        "title": str(metadata.get("title") or ""),
        "creators": [str(value) for value in metadata.get("creators", [])],
        "series": str(metadata.get("series") or ""),
        "series_index": str(metadata.get("series_index") or ""),
        "resource_count": len(resources),
        "resource_manifest": resources,
        "resource_digest": _json_digest(resources),
        "chapter_count": sum(
            name.casefold().endswith((".xhtml", ".html", ".htm")) for name in resources
        ),
        "image_count": sum(
            name.casefold().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))
            for name in resources
        ),
        "package_structure_digest": _json_digest(structure),
    }


def compare_publications(old: Path, new: Path, config: AppConfig) -> dict[str, Any]:
    old_record = inspect_publication(old, config)
    new_record = inspect_publication(new, config)
    same_inode = (old_record["device"], old_record["inode"]) == (
        new_record["device"],
        new_record["inode"],
    )
    title_matches = _normal(old_record["title"]) == _normal(new_record["title"])
    creators_match = {_normal(value) for value in old_record["creators"]} == {
        _normal(value) for value in new_record["creators"]
    }
    resources_match = old_record["resource_manifest"] == new_record["resource_manifest"]
    exact_bytes = old_record["sha256"] == new_record["sha256"]
    valid = old_record["epub_status"] == new_record["epub_status"] == "ok"
    archivable = valid and title_matches and creators_match and resources_match and not same_inode
    if archivable:
        outcome = "exact-copy" if exact_bytes else "metadata-only-rewrite"
    elif not valid:
        outcome = "invalid-epub-manual-review"
    elif not title_matches or not creators_match:
        outcome = "work-identity-conflict-manual-review"
    elif not resources_match:
        outcome = "publication-content-differs-manual-review"
    else:
        outcome = "unsafe-file-identity-manual-review"
    return {
        "old": old_record,
        "new": new_record,
        "comparison": {
            "exact_bytes": exact_bytes,
            "same_inode": same_inode,
            "title_matches": title_matches,
            "creators_match": creators_match,
            "resources_match": resources_match,
            "package_structure_matches": (
                old_record["package_structure_digest"]
                == new_record["package_structure_digest"]
            ),
            "outcome": outcome,
            "archivable": archivable,
        },
    }


def create_plan(
    pairs: Iterable[tuple[Path, Path]],
    archive_root: Path,
    output: Path,
    config: AppConfig,
    *,
    state_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    if output.exists() or output.is_symlink():
        raise ReconciliationRefused("plan output already exists")
    root = archive_root.expanduser().resolve(strict=False)
    _require_archive_separation(root, config)
    plan_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(5)
    state = state_root or AppPaths.default().state_dir
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for old, new in pairs:
        analysis = compare_publications(old, new, config)
        old_path = Path(analysis["old"]["path"])
        new_path = Path(analysis["new"]["path"])
        library_root = _library_root(old_path, config)
        if _library_root(new_path, config) != library_root:
            raise ReconciliationRefused("old and new publications are in different libraries")
        if old_path in seen or new_path in seen:
            raise ReconciliationRefused("a publication appears in more than one pair")
        seen.update((old_path, new_path))
        relative = old_path.relative_to(library_root)
        provenance = _pair_provenance(
            state,
            old_path,
            new_path,
            str(analysis["old"]["sha256"]),
            str(analysis["new"]["sha256"]),
            config,
        )
        item_id = hashlib.sha256(f"{old_path}\0{new_path}".encode()).hexdigest()[:16]
        items.append(
            {
                "item_id": item_id,
                **analysis,
                "archive": str(root / plan_id / relative),
                "library_root": str(library_root),
                "provenance": provenance,
            }
        )
    if not items:
        raise ReconciliationRefused("at least one old/new pair is required")
    plan = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "created_at": datetime.now(UTC).isoformat(),
        "archive_root": str(root),
        "items": items,
    }
    digest = _json_digest(plan)
    _create_private_json(output, {**plan, "integrity_sha256": digest}, mode=0o400)
    return plan, digest


def create_state_template(plan_path: Path, output: Path) -> Path:
    plan, digest = load_plan(plan_path)
    payload = {
        "schema": STATE_SCHEMA,
        "plan_sha256": digest,
        "reviewed_at": None,
        "items": [
            {
                "item_id": item["item_id"],
                "old_kavita_id": None,
                "new_kavita_id": None,
                "reading_progress": "unknown",
                "custom_cover": "unknown",
                "resolution_note": "",
            }
            for item in plan["items"]
        ],
    }
    _create_private_json(output, payload, mode=0o600)
    return output


def approve_plan(
    plan_path: Path,
    digest: str,
    state_report: Path,
) -> Path:
    plan, actual = load_plan(plan_path)
    if not secrets.compare_digest(actual, digest):
        raise ReconciliationRefused("approval digest does not match the immutable plan")
    state = _load_state_report(state_report, plan, digest)
    blocked = [
        item["item_id"]
        for item in plan["items"]
        if not item["comparison"]["archivable"] or not item["provenance"]["same_source"]
    ]
    if blocked:
        raise ReconciliationRefused(
            "manual-review items cannot be approved: " + ", ".join(blocked)
        )
    unresolved = [
        item["item_id"]
        for item in state["items"]
        if item.get("reading_progress") not in {"none", "preserved-manually"}
        or item.get("custom_cover") not in {"none", "preserved-manually"}
        or type(item.get("old_kavita_id")) is not int
        or int(item["old_kavita_id"]) <= 0
        or type(item.get("new_kavita_id")) is not int
        or int(item["new_kavita_id"]) <= 0
        or item.get("old_kavita_id") == item.get("new_kavita_id")
        or not str(item.get("resolution_note") or "").strip()
    ]
    if unresolved:
        raise ReconciliationRefused(
            "Kavita progress/cover review remains unresolved: " + ", ".join(unresolved)
        )
    approval = plan_path.with_suffix(plan_path.suffix + ".approval.json")
    payload = {
        "schema": APPROVAL_SCHEMA,
        "plan_sha256": digest,
        "state_report": str(state_report.expanduser().resolve(strict=True)),
        "state_report_sha256": sha256_file(state_report),
        "approved_at": datetime.now(UTC).isoformat(),
    }
    _create_private_json(approval, payload, mode=0o400)
    return approval


def preview(plan_path: Path) -> dict[str, Any]:
    plan, digest = load_plan(plan_path)
    states = []
    for item in plan["items"]:
        old = Path(item["old"]["path"])
        new = Path(item["new"]["path"])
        archive = Path(item["archive"])
        states.append(
            {
                "item_id": item["item_id"],
                "old": str(old),
                "new": str(new),
                "archive": str(archive),
                "old_exists": old.is_file() and not old.is_symlink(),
                "new_exists": new.is_file() and not new.is_symlink(),
                "archive_exists": archive.exists() or archive.is_symlink(),
                "outcome": item["comparison"]["outcome"],
            }
        )
    return {"plan_id": plan["plan_id"], "digest": digest, "items": states}


def execute(plan_path: Path, config: AppConfig, *, rollback: bool = False) -> dict[str, Any]:
    plan, digest = load_plan(plan_path)
    _require_approval(plan_path, plan, digest)
    _require_no_active_ingest(config)
    root = _existing_archive_root(Path(plan["archive_root"]), config)
    journal_path = plan_path.with_suffix(plan_path.suffix + ".journal.json")
    lock_path = plan_path.with_suffix(plan_path.suffix + ".lock")
    with ProcessLock(lock_path):
        journal = _load_or_create_journal(journal_path, plan, digest)
        _preflight(plan, journal, root, rollback=rollback)
        for item in plan["items"]:
            row = next(value for value in journal["items"] if value["item_id"] == item["item_id"])
            _move_one(item, row, rollback=rollback)
            journal["updated_at"] = datetime.now(UTC).isoformat()
            _replace_private_json(journal_path, journal)
        complete_state = "rolled_back" if rollback else "complete"
        journal["state"] = complete_state
        journal["updated_at"] = datetime.now(UTC).isoformat()
        _replace_private_json(journal_path, journal)
        if not rollback:
            _write_supersession_report(plan, digest, journal, config)
        return journal


def load_plan(path: Path) -> tuple[dict[str, Any], str]:
    payload = _load_private_json(path)
    if payload.get("schema") != PLAN_SCHEMA or not isinstance(payload.get("items"), list):
        raise ReconciliationRefused("unsupported reconciliation plan")
    integrity = payload.pop("integrity_sha256", None)
    actual = _json_digest(payload)
    payload["integrity_sha256"] = integrity
    if not isinstance(integrity, str) or not secrets.compare_digest(integrity, actual):
        raise ReconciliationRefused("reconciliation plan integrity check failed")
    payload.pop("integrity_sha256")
    return payload, actual


def _pair_provenance(
    state_root: Path,
    old: Path,
    new: Path,
    old_sha256: str,
    new_sha256: str,
    config: AppConfig,
) -> dict[str, Any]:
    links: dict[Path, list[dict[str, str]]] = {old: [], new: []}
    expected_hashes = {old: old_sha256, new: new_sha256}
    report_root = state_root / "kavita-reports"
    if report_root.is_dir() and not report_root.is_symlink():
        for report in sorted(report_root.glob("*.json")):
            try:
                report_info = report.lstat()
                if (
                    not stat.S_ISREG(report_info.st_mode)
                    or stat.S_ISLNK(report_info.st_mode)
                    or report_info.st_nlink != 1
                ):
                    continue
                payload = json.loads(report.read_text(encoding="utf-8"))
                schema = payload.get("report_schema")
                if not _accepted_publication_report(payload, schema):
                    continue
                for index, item in enumerate(payload.get("items", []), 1):
                    if not _accepted_publication_item(item, schema):
                        continue
                    destination = Path(str(item.get("destination")))
                    if (
                        destination in links
                        and item.get("destination_sha256") == expected_hashes[destination]
                    ):
                        links[destination].append(
                            {
                                "source": str(item.get("source")),
                                "source_sha256": str(item.get("source_sha256")),
                                "destination_sha256": str(item.get("destination_sha256")),
                                "report": str(report),
                                "record": f"{report}:{index}",
                            }
                        )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    old_links, new_links = links[old], links[new]
    old_identities = {(item["source"], item["source_sha256"]) for item in old_links}
    new_identities = {(item["source"], item["source_sha256"]) for item in new_links}
    common = old_identities & new_identities
    if len(common) != 1:
        return {
            "same_source": False,
            "reason": "accepted reports do not bind one exact source and both current destinations",
            "old_records": old_links,
            "new_records": new_links,
        }
    source, source_hash = next(iter(common))
    try:
        source_path = _regular(Path(source))
        incoming_roots = [value.resolve(strict=True) for value in config.incoming_roots]
        if not any(source_path.is_relative_to(value) for value in incoming_roots):
            raise ReconciliationRefused("reported source is outside configured Incoming roots")
        if sha256_file(source_path) != source_hash:
            raise ReconciliationRefused("reported source hash no longer matches")
    except (OSError, ReconciliationRefused):
        return {
            "same_source": False,
            "reason": "reported Incoming source is missing, unsafe, or changed",
            "old_records": old_links,
            "new_records": new_links,
        }
    return {
        "same_source": True,
        "source": source,
        "source_sha256": source_hash,
        "old_records": [
            item["record"]
            for item in old_links
            if (item["source"], item["source_sha256"]) == (source, source_hash)
        ],
        "new_records": [
            item["record"]
            for item in new_links
            if (item["source"], item["source_sha256"]) == (source, source_hash)
        ],
    }


def _accepted_publication_report(payload: object, schema: object) -> bool:
    if not isinstance(payload, dict) or schema not in {
        "kavita-ingest-publication-v1",
        "kavita-ingest-publication-v2",
        "kavita-ingest-publication-v3",
    }:
        return False
    accepted_lifecycle = {"preserved"} if schema == "kavita-ingest-publication-v1" else {
        "preserved",
        "mixed",
    }
    recovery_consistent = (
        schema == "kavita-ingest-publication-v3"
        and payload.get("status") == "partial"
        and payload.get("recovery_required") is True
    ) or (
        payload.get("status") == "completed"
        and not payload.get("recovery_required")
    )
    return bool(
        recovery_consistent
        and payload.get("approved_apply") is True
        and payload.get("source_lifecycle") in accepted_lifecycle
        and payload.get("verification_completed") is True
    )


def _accepted_publication_item(item: object, schema: object) -> bool:
    if not isinstance(item, dict):
        return False
    if schema == "kavita-ingest-publication-v3" and (
        item.get("item_state") != "complete" or item.get("cleanup_eligible") is not True
    ):
        return False
    return not (
        schema in {"kavita-ingest-publication-v2", "kavita-ingest-publication-v3"}
        and (
            item.get("source_lifecycle") != "preserved"
            or item.get("action") not in {"published", "verified-existing-duplicate"}
        )
    )


def _preflight(
    plan: dict[str, Any], journal: dict[str, Any], root: Path, *, rollback: bool
) -> None:
    for item in plan["items"]:
        row = next(value for value in journal["items"] if value["item_id"] == item["item_id"])
        old, new, archive = (
            Path(item["old"]["path"]),
            Path(item["new"]["path"]),
            Path(item["archive"]),
        )
        if not archive.is_relative_to(root):
            raise ReconciliationRefused("archive path escapes configured archive root")
        _require_hash(new, item["new"]["sha256"], "canonical publication")
        if rollback:
            if row["state"] == "rolled_back":
                _require_hash(old, item["old"]["sha256"], "restored publication")
            else:
                _require_hash(archive, item["old"]["sha256"], "archived publication")
                if old.exists() or old.is_symlink():
                    raise ReconciliationRefused(f"rollback destination already exists: {old}")
        elif row["state"] == "moved":
            _require_hash(archive, item["old"]["sha256"], "archived publication")
            if old.exists() or old.is_symlink():
                raise ReconciliationRefused(f"both old and archived publications exist: {old}")
        elif old.exists() and not archive.exists() and not archive.is_symlink():
            _require_hash(old, item["old"]["sha256"], "superseded publication")
            if old.stat().st_dev != root.stat().st_dev:
                raise ReconciliationRefused(
                    "archive must be on the same filesystem for atomic move"
                )
        elif not old.exists() and archive.is_file() and not archive.is_symlink():
            _require_hash(archive, item["old"]["sha256"], "archived publication")
            row["state"] = "moved"
        else:
            raise ReconciliationRefused(f"unsafe old/archive state for {old}")


def _move_one(item: dict[str, Any], row: dict[str, Any], *, rollback: bool) -> None:
    old, archive = Path(item["old"]["path"]), Path(item["archive"])
    if rollback:
        if row["state"] == "rolled_back":
            return
        old.parent.mkdir(parents=True, exist_ok=True)
        archive.rename(old)
        _fsync_directories(archive.parent, old.parent)
        _require_hash(old, item["old"]["sha256"], "restored publication")
        row["state"] = "rolled_back"
        return
    if row["state"] == "moved":
        return
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive.exists() or archive.is_symlink():
        raise ReconciliationRefused(f"archive destination collision: {archive}")
    old.rename(archive)
    _fsync_directories(old.parent, archive.parent)
    _require_hash(archive, item["old"]["sha256"], "archived publication")
    row["state"] = "moved"


def _write_supersession_report(
    plan: dict[str, Any],
    digest: str,
    journal: dict[str, Any],
    config: AppConfig,
) -> Path:
    if config.database_path is None:
        raise ReconciliationRefused("state database is not configured")
    root = config.database_path.parent / "supersession-reports"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{plan['plan_id']}.json"
    payload = {
        "report_schema": REPORT_SCHEMA,
        "plan_id": plan["plan_id"],
        "plan_sha256": digest,
        "state": journal["state"],
        "completed_at": journal["updated_at"],
        "items": [
            {
                "item_id": item["item_id"],
                "source": item["provenance"].get("source"),
                "source_sha256": item["provenance"].get("source_sha256"),
                "old_destination": item["old"]["path"],
                "old_destination_sha256": item["old"]["sha256"],
                "archive": item["archive"],
                "canonical_destination": item["new"]["path"],
                "canonical_destination_sha256": item["new"]["sha256"],
                "old_records": item["provenance"].get("old_records"),
                "new_records": item["provenance"].get("new_records"),
            }
            for item in plan["items"]
        ],
    }
    if target.exists():
        existing = _load_private_json(target)
        payload["completed_at"] = existing.get("completed_at")
        if existing != payload:
            raise ReconciliationRefused("existing supersession report differs")
        return target
    _create_private_json(target, payload, mode=0o400)
    return target


def _load_state_report(path: Path, plan: dict[str, Any], digest: str) -> dict[str, Any]:
    payload = _load_private_json(path, require_private=False)
    if payload.get("schema") != STATE_SCHEMA or payload.get("plan_sha256") != digest:
        raise ReconciliationRefused("Kavita state report is for a different plan")
    expected = {item["item_id"] for item in plan["items"]}
    items = payload.get("items")
    if not isinstance(items, list) or {item.get("item_id") for item in items} != expected:
        raise ReconciliationRefused("Kavita state report item set is incomplete")
    if not payload.get("reviewed_at"):
        raise ReconciliationRefused("Kavita state report has not been reviewed")
    return payload


def _require_approval(path: Path, plan: dict[str, Any], digest: str) -> None:
    approval_path = path.with_suffix(path.suffix + ".approval.json")
    approval = _load_private_json(approval_path)
    if approval.get("schema") != APPROVAL_SCHEMA or approval.get("plan_sha256") != digest:
        raise ReconciliationRefused("plan is not approved for this digest")
    state_path = Path(str(approval.get("state_report")))
    if sha256_file(state_path) != approval.get("state_report_sha256"):
        raise ReconciliationRefused("approved Kavita state report changed")
    _load_state_report(state_path, plan, digest)


def _require_no_active_ingest(config: AppConfig) -> None:
    if config.database_path is None or not config.database_path.is_file():
        raise ReconciliationRefused("ingestion state database is unavailable")
    uri = f"file:{config.database_path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        active = connection.execute(
            "SELECT count(*) FROM apply_runs WHERE status IN "
            "('preflighting','running','recovery_required')"
        ).fetchone()[0]
    if active:
        raise ReconciliationRefused("an ingestion apply or recovery run is active")


def _load_or_create_journal(
    path: Path, plan: dict[str, Any], digest: str
) -> dict[str, Any]:
    if path.exists():
        payload = _load_private_json(path)
        if payload.get("schema") != JOURNAL_SCHEMA or payload.get("plan_sha256") != digest:
            raise ReconciliationRefused("journal does not match plan")
        return payload
    payload = {
        "schema": JOURNAL_SCHEMA,
        "plan_id": plan["plan_id"],
        "plan_sha256": digest,
        "state": "running",
        "updated_at": datetime.now(UTC).isoformat(),
        "items": [{"item_id": item["item_id"], "state": "pending"} for item in plan["items"]],
    }
    _create_private_json(path, payload, mode=0o600)
    return payload


def _existing_archive_root(path: Path, config: AppConfig) -> Path:
    _require_archive_separation(path, config)
    root = path.resolve(strict=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ReconciliationRefused("archive root must be a real directory")
    if not os.access(root, os.W_OK | os.X_OK):
        raise ReconciliationRefused("archive root is not writable by the current operator")
    return root


def _require_archive_separation(root: Path, config: AppConfig) -> None:
    resolved = root.resolve(strict=False)
    protected = [*config.incoming_roots]
    protected.extend(path for path in (config.books_root, config.comics_root) if path)
    for value in protected:
        other = value.resolve(strict=False)
        if resolved == other or resolved.is_relative_to(other) or other.is_relative_to(resolved):
            raise ReconciliationRefused("archive root must be separate from Incoming and libraries")


def _library_root(path: Path, config: AppConfig) -> Path:
    matches: list[Path] = []
    for root in (config.books_root, config.comics_root):
        if root is None:
            continue
        candidate = root.resolve(strict=False)
        if path.is_relative_to(candidate):
            matches.append(root.resolve(strict=True))
    if len(matches) != 1:
        raise ReconciliationRefused("publication is not inside exactly one configured library")
    return matches[0]


def _regular(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    info = path.expanduser().lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ReconciliationRefused("publication must be a regular non-symlink single-link file")
    return resolved


def _require_hash(path: Path, digest: str, label: str) -> None:
    actual = _regular(path)
    if sha256_file(actual) != digest:
        raise ReconciliationRefused(f"{label} changed after plan: {path}")


def _load_private_json(path: Path, *, require_private: bool = True) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ReconciliationRefused("state file is not a regular non-symlink file")
    if require_private and info.st_mode & 0o077:
        raise ReconciliationRefused("state file permissions are not private")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReconciliationRefused("state file is not a JSON object")
    return payload


def _create_private_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, mode)
    _fsync_directories(path.parent)


def _replace_private_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _create_private_json(temporary, payload, mode=0o600)
        os.replace(temporary, path)
        _fsync_directories(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directories(*paths: Path) -> None:
    for path in dict.fromkeys(paths):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normal(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("’", "'")
    normalized = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(normalized.split())
