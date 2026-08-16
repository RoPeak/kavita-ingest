from __future__ import annotations

import shlex
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import rarfile
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .comicinfo import PLANNED_COMICINFO_PROFILE
from .config import AppConfig
from .discovery import detect_signature
from .domain import InspectionStatus, SourceFormat
from .filesystem import LinuxFilesystem, sha256_file
from .inspectors import inspect
from .library_check import LibraryCheckResult, LibraryFinding, LibraryScope
from .writers.comic import write_cbz_metadata
from .writers.repack import repack_cbr_to_cbz

RepairKind = Literal["move", "rewrite_cbz", "repack_cbr"]


@dataclass(frozen=True, slots=True)
class LibraryRepairAction:
    source: Path
    destination: Path
    library_root: Path
    kind: RepairKind
    reason_codes: tuple[str, ...]
    source_hash: str
    set_fields: tuple[tuple[str, str], ...] = ()
    clear_fields: tuple[str, ...] = ()

    @property
    def changes_metadata(self) -> bool:
        return bool(self.set_fields or self.clear_fields)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "kind": self.kind,
            "reason_codes": list(self.reason_codes),
            "source_hash": self.source_hash,
            "set_fields": dict(self.set_fields),
            "clear_fields": list(self.clear_fields),
        }


@dataclass(frozen=True, slots=True)
class LibraryRepairAdvice:
    path: Path
    finding_codes: tuple[str, ...]
    guidance: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "finding_codes": list(self.finding_codes),
            "guidance": list(self.guidance),
        }


@dataclass(frozen=True, slots=True)
class LibraryRepairPlan:
    actions: tuple[LibraryRepairAction, ...]
    manual: tuple[LibraryRepairAdvice, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "manual": [advice.to_dict() for advice in self.manual],
        }


@dataclass(frozen=True, slots=True)
class LibraryRepairOutcome:
    completed: tuple[LibraryRepairAction, ...]


_SAFE_PATH_CODES = frozenset(
    {
        "media_at_library_root",
        "comic_series_folder_mismatch",
        "noncanonical_book_path",
        "noncanonical_comic_folder",
        "noncanonical_comic_path",
    }
)
_SAFE_METADATA_CODES = frozenset({"collection_format_uses_issue_number"})
_SAFE_CONTAINER_CODES = frozenset({"noncanonical_cbr_container"})
_SAFE_CODES = _SAFE_PATH_CODES | _SAFE_METADATA_CODES | _SAFE_CONTAINER_CODES


def plan_library_repairs(result: LibraryCheckResult, config: AppConfig) -> LibraryRepairPlan:
    """Build a conservative, hash-bound repair plan from an already-read-only audit.

    Only findings whose correct result is fully determined by existing local metadata are
    eligible.  Anything that requires choosing between identities, inventing metadata,
    deleting duplicates, following symlinks, or interpreting corrupt media remains manual.
    """

    grouped: dict[Path, list[LibraryFinding]] = defaultdict(list)
    for finding in result.findings:
        grouped[finding.path].append(finding)

    actions: list[LibraryRepairAction] = []
    manual: list[LibraryRepairAdvice] = []

    for path in sorted(grouped, key=lambda item: str(item).casefold()):
        findings = grouped[path]
        if not path.is_file() or path.is_symlink():
            manual.append(_manual_advice(path, findings, config))
            continue
        codes = {finding.code for finding in findings}
        if codes - _SAFE_CODES:
            manual.append(_manual_advice(path, findings, config))
            continue

        target = _full_expected_target(path, findings, result.scopes)
        metadata = (
            _collection_number_migration(path, config)
            if codes & _SAFE_METADATA_CODES
            else None
        )
        detected = detect_signature(path)[1]
        needs_repack = detected is SourceFormat.CBR or "noncanonical_cbr_container" in codes

        if metadata is None and codes & _SAFE_METADATA_CODES:
            manual.append(_manual_advice(path, findings, config))
            continue
        if target is None:
            # Safe metadata-only replacement is intentionally not attempted without a durable
            # swap journal.  Leave it as explicit guidance rather than risking an in-place edit.
            manual.append(_manual_advice(path, findings, config))
            continue
        if target == path and not needs_repack and metadata is None:
            continue
        if target == path and (needs_repack or metadata is not None):
            manual.append(_manual_advice(path, findings, config))
            continue

        scope = _scope_for(path, result.scopes)
        if scope is None:
            manual.append(_manual_advice(path, findings, config))
            continue
        action = LibraryRepairAction(
            source=path,
            destination=target,
            library_root=scope.library_root,
            kind=(
                "repack_cbr"
                if needs_repack
                else "rewrite_cbz"
                if metadata is not None
                else "move"
            ),
            reason_codes=tuple(sorted(codes)),
            source_hash=sha256_file(path),
            set_fields=tuple(sorted((metadata or {}).items())),
            clear_fields=("Number",) if metadata is not None else (),
        )
        actions.append(action)

    actions, collision_manual = _remove_colliding_actions(actions)
    manual.extend(collision_manual)
    manual.sort(key=lambda item: str(item.path).casefold())
    actions.sort(key=lambda item: str(item.source).casefold())
    return LibraryRepairPlan(tuple(actions), tuple(manual))


def apply_library_repairs(plan: LibraryRepairPlan, config: AppConfig) -> LibraryRepairOutcome:
    """Apply only the precomputed safe actions, refusing changed sources or collisions."""

    filesystem = LinuxFilesystem()
    completed: list[LibraryRepairAction] = []
    for action in plan.actions:
        _validate_action_preconditions(action)
        filesystem.ensure_directory(
            action.library_root,
            action.destination.parent,
            config.created_directory_mode,
        )
        if action.kind == "move":
            # LinuxFilesystem.commit creates the destination with a no-clobber hard link,
            # durably syncs it, and only then removes the old pathname.
            filesystem.commit(action.source, action.destination)
        else:
            set_fields: dict[str, object] = {key: value for key, value in action.set_fields}
            with tempfile.TemporaryDirectory(
                prefix=".kavita-ingest-library-fix-", dir=action.destination.parent
            ) as temporary:
                staged = Path(temporary) / action.destination.name
                try:
                    if action.kind == "rewrite_cbz":
                        write_cbz_metadata(
                            action.source,
                            staged,
                            set_fields=set_fields,
                            clear_fields=action.clear_fields,
                            comicinfo_profile=PLANNED_COMICINFO_PROFILE,
                        )
                    else:
                        repack_cbr_to_cbz(
                            action.source,
                            staged,
                            set_fields=set_fields,
                            clear_fields=action.clear_fields,
                            limits=config.archive_limits(),
                            comicinfo_profile=PLANNED_COMICINFO_PROFILE,
                        )
                except (OSError, ValueError, zipfile.BadZipFile, rarfile.Error) as exc:
                    raise ValueError(
                        f"safe transformation failed for {action.source}: {exc}"
                    ) from exc
                filesystem.set_file_mode(staged, config.published_file_mode)
                filesystem.make_file_durable(staged)
                if sha256_file(action.source) != action.source_hash:
                    raise ValueError(f"source changed while repair was staged: {action.source}")
                filesystem.commit(staged, action.destination)
            # The transformed destination has already been verified by the writer and published
            # no-clobber.  Remove the original only after rechecking its plan-bound hash.
            if sha256_file(action.source) != action.source_hash:
                raise ValueError(
                    f"source changed after repaired destination publication; original retained: "
                    f"{action.source}"
                )
            filesystem.durable_unlink(action.source)
        _remove_empty_parents(action.source.parent, action.library_root)
        completed.append(action)
    return LibraryRepairOutcome(tuple(completed))


def render_library_repair_summary(plan: LibraryRepairPlan, console: Console) -> None:
    if not plan.actions and not plan.manual:
        return
    console.print("\n[bold]Next steps[/bold]")
    if plan.actions:
        console.print(
            f"[bold green]✓ {len(plan.actions)} file{'s' if len(plan.actions) != 1 else ''} "
            "can be repaired automatically[/bold green]"
        )
        console.print("  kavita-ingest can make only changes whose target is unambiguous.")
    if plan.manual:
        console.print(
            f"[bold orange3]⚠ {len(plan.manual)} item{'s' if len(plan.manual) != 1 else ''} "
            "need human judgement[/bold orange3]"
        )
        console.print("  The program will explain what to do, but will not guess.")


def render_library_repair_plan(plan: LibraryRepairPlan, console: Console) -> None:
    body = Text()
    body.append(
        f"{len(plan.actions)} automatic repair{'s' if len(plan.actions) != 1 else ''}\n",
        style="bold",
    )
    body.append("No provider lookups. No overwrites. Source hashes are checked before changes.")
    console.print(Panel(body, title="Safe library repair plan", border_style="cyan"))
    for index, action in enumerate(plan.actions, start=1):
        console.print(f"\n[bold]{index}. {_short_path(action.source)}[/bold]")
        if action.kind == "move":
            console.print("   [green]Move/rename only[/green]")
        elif action.kind == "rewrite_cbz":
            console.print("   [green]Update ComicInfo safely, then move/rename[/green]")
        else:
            console.print("   [green]Convert CBR/RAR to CBZ safely, then move/rename[/green]")
        for field, value in action.set_fields:
            console.print(f"   Metadata: set {field} = {value}")
        for field in action.clear_fields:
            console.print(f"   Metadata: clear {field}")
        console.print(f"   From: {action.source}")
        console.print(f"   To:   {action.destination}")


def render_manual_guidance(plan: LibraryRepairPlan, console: Console) -> None:
    if not plan.manual:
        return
    console.print("\n[bold orange3]Manual guidance[/bold orange3]")
    for advice in plan.manual:
        console.print(f"\n[bold]• {advice.path}[/bold]")
        for line in advice.guidance:
            console.print(f"  {line}")


def _collection_number_migration(path: Path, config: AppConfig) -> dict[str, str] | None:
    try:
        _signature, detected = detect_signature(path)
        if detected not in {SourceFormat.CBZ, SourceFormat.CBR}:
            return None
        inspection = inspect(path, detected, config.archive_limits())
    except (OSError, ValueError):
        return None
    if inspection.status is not InspectionStatus.OK:
        return None
    comicinfo = inspection.metadata.get("comicinfo")
    if not isinstance(comicinfo, dict):
        return None
    number = str(comicinfo.get("Number") or "").strip()
    volume = str(comicinfo.get("Volume") or "").strip()
    format_value = str(comicinfo.get("Format") or "").strip().casefold()
    if volume or format_value not in {"trade paperback", "omnibus"} or not number.isdigit():
        return None
    return {"Volume": str(int(number))}


def _full_expected_target(
    path: Path,
    findings: list[LibraryFinding],
    scopes: tuple[LibraryScope, ...],
) -> Path | None:
    preferred = ("noncanonical_book_path", "noncanonical_comic_path")
    finding = next(
        (
            item
            for code in preferred
            for item in findings
            if item.code == code and item.expected
        ),
        None,
    )
    if finding is None:
        return None
    scope = _scope_for(path, scopes)
    if scope is None:
        return None
    target = (scope.library_root / str(finding.expected)).resolve(strict=False)
    if not target.is_relative_to(scope.library_root):
        return None
    return target


def _scope_for(path: Path, scopes: tuple[LibraryScope, ...]) -> LibraryScope | None:
    resolved = path.resolve(strict=False)
    for scope in scopes:
        if resolved == scope.library_root or resolved.is_relative_to(scope.library_root):
            return scope
    return None


def _remove_colliding_actions(
    actions: list[LibraryRepairAction],
) -> tuple[list[LibraryRepairAction], list[LibraryRepairAdvice]]:
    by_target: dict[str, list[LibraryRepairAction]] = defaultdict(list)
    for action in actions:
        by_target[_collision_key(action.destination)].append(action)
    safe: list[LibraryRepairAction] = []
    manual: list[LibraryRepairAdvice] = []
    for action in actions:
        competing = by_target[_collision_key(action.destination)]
        collision = len(competing) > 1
        if not collision and action.destination.exists() and action.destination != action.source:
            collision = True
        if not collision and action.destination.parent.exists():
            # Refuse a case-insensitive collision with any other existing sibling.
            key = action.destination.name.casefold()
            collision = any(
                child.name.casefold() == key and child != action.source
                for child in action.destination.parent.iterdir()
            )
        if collision:
            manual.append(
                LibraryRepairAdvice(
                    action.source,
                    action.reason_codes,
                    (
                        "Automatic repair was skipped because the recommended destination already "
                        "exists or another file wants the same destination.",
                        "Compare the files manually; kavita-ingest will never overwrite or "
                        "delete a possible duplicate automatically.",
                    ),
                )
            )
        else:
            safe.append(action)
    return safe, manual


def _validate_action_preconditions(action: LibraryRepairAction) -> None:
    if not action.source.is_file() or action.source.is_symlink():
        raise ValueError(
            f"repair source is unavailable or no longer a regular file: {action.source}"
        )
    if sha256_file(action.source) != action.source_hash:
        raise ValueError(f"repair source changed since the plan was built: {action.source}")
    if action.destination.exists() and action.destination != action.source:
        raise ValueError(f"repair destination already exists: {action.destination}")
    if not action.destination.resolve(strict=False).is_relative_to(action.library_root):
        raise ValueError(
            f"repair destination escapes configured library root: {action.destination}"
        )


def _remove_empty_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _manual_advice(
    path: Path, findings: list[LibraryFinding], config: AppConfig
) -> LibraryRepairAdvice:
    codes = tuple(sorted({item.code for item in findings}))
    lines: list[str] = []
    expected = next(
        (item.expected for item in findings if item.expected and "path" in item.code), None
    )
    if "comic_title_is_only_edition_label" in codes:
        lines.extend(
            (
                "The title needs a human decision; kavita-ingest will not invent a "
                "descriptive title.",
                "Recommended: reset this verified publication back to Incoming, re-review it, and "
                "enter/accept the real collected-edition title.",
                _reset_published_guidance(path, config),
            )
        )
    if "duplicate_comic_slot" in codes:
        lines.extend(
            (
                "Two files claim the same issue/volume. Compare their contents and metadata before "
                "keeping, moving, or deleting either one.",
                "Automatic repair will never choose which duplicate is authoritative.",
            )
        )
    if "mixed_series_folder" in codes or "series_split_across_folders" in codes:
        lines.append(
            "Resolve which series folder is authoritative first, then rerun the library check."
        )
    if "comicinfo_missing" in codes:
        lines.append(
            "Reprocess the comic through kavita-ingest so ComicInfo.xml can be written from an "
            "explicitly reviewed identity."
        )
    if "book_title_metadata_missing" in codes or "book_creator_metadata_missing" in codes:
        lines.append(
            "Reprocess the book through kavita-ingest or correct its embedded metadata "
            "before renaming."
        )
    if "unreadable_media" in codes or "inspection_failed" in codes:
        lines.append("Restore or replace the damaged/unreadable file before attempting any rename.")
    if "extension_signature_mismatch" in codes or "unsupported_signature" in codes:
        lines.append(
            "Confirm the real container/file format before changing the extension; the fixer "
            "will not rename a file whose bytes disagree with its name."
        )
    if "library_kind_mismatch" in codes:
        lines.append(
            "Review whether this belongs in Books or Comics, then ingest/move it through "
            "the correct library."
        )
    if "symlink_directory_skipped" in codes or "symlink_file_skipped" in codes:
        lines.append(
            "Audit the real symlink target separately or replace the link with a managed "
            "media file."
        )
    if "collection_format_uses_issue_number" in codes:
        lines.append(
            "For a collected edition, set ComicInfo Volume to the integer collection volume, clear "
            "Number, and use the vNN filename form."
        )
    if "comic_number_and_volume_set" in codes:
        lines.append(
            "For a collected edition, keep Volume and clear Number once you have confirmed "
            "the volume."
        )
    if "noninteger_collection_volume" in codes or "invalid_comic_number" in codes:
        lines.append(
            "Correct the issue/volume value manually; kavita-ingest will not guess a number."
        )
    if "comic_sequence_missing" in codes:
        lines.append("Identify the issue/volume number before canonical renaming.")
    if "collection_volume_without_format" in codes or "noncanonical_comic_format" in codes:
        lines.append(
            "Confirm the collected-edition Format before changing Volume/Number semantics."
        )
    if expected:
        lines.append(f"Once the metadata is correct, the recommended path is: {expected}")
    if not lines:
        lines.append(
            "This finding is not safe to automate. Review the file and its metadata, correct the "
            "underlying issue, then rerun the library check."
        )
    return LibraryRepairAdvice(path, codes, tuple(dict.fromkeys(lines)))



def _reset_published_guidance(path: Path, config: AppConfig) -> str:
    kind = "Comics"
    if config.books_root is not None and path.resolve(strict=False).is_relative_to(
        config.books_root.expanduser().resolve(strict=False)
    ):
        kind = "Books"
    placeholder = f"{kind}/CORRECTED-NAME{path.suffix.casefold()}"
    return (
        "If this was published by kavita-ingest, a safe reset command is: "
        f"ki reset-published {shlex.quote(str(path))} --to {shlex.quote(placeholder)}"
    )

def _collision_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _short_path(path: Path) -> str:
    return path.name if len(str(path)) > 90 else str(path)
