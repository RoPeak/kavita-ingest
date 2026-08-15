# Kavita Ingest

Kavita Ingest is a safety-focused Linux CLI for identifying, reviewing, planning,
and organizing ebooks and digital comics into Kavita-friendly libraries. It
supports explicit metadata matching, immutable offline plans, verified staged
writes, atomic no-overwrite publication, and crash recovery.

It is designed for a deliberate workflow: the program may rank a match, but a
person accepts the identity and separately approves the exact filesystem plan.

## Scope

Supported inputs and transformations:

| Input | Inspection | Metadata output | Notes |
| --- | --- | --- | --- |
| EPUB | Yes | EPUB/OPF | Calibre plus narrow contributor-role OPF patching |
| PDF | Yes | Calibre XMP | Books and comic issues; unsigned, unencrypted, non-signature-bearing PDFs only; semantic payload verified |
| CBZ | Yes | CBZ + rich ComicInfo 2.1 | Existing publication payloads preserved |
| CBR/RAR3 | Yes | Repacked CBZ + ComicInfo | Single-volume ordinary archives |
| CBR/RAR5 | Yes | Repacked CBZ + ComicInfo | Single-volume ordinary archives |

Book output targets a Kavita **Books** library. Comics target Kavita **Comic
(Flexible)**; projected ComicInfo `Series` includes a run-start year when needed
to distinguish same-named runs.

### Kavita comic library target

The current comic projection contract deliberately targets **Comic (Flexible)**, not
Kavita's stricter **Comic** library type. For a regular issue whose canonical
identity is `series_title = "Absolute Batman"` and `run_start_year = 2024`, the
projection is:

```text
ComicInfo.Series = Absolute Batman (2024)
ComicInfo.Volume = omitted/cleared unless it is a real collection volume
folder           = Absolute Batman (2024)/
```

This keeps the provider run-start year separate from ComicInfo's collection
`Volume` meaning while giving Comic (Flexible) the run disambiguation it needs.
Kavita's current library guidance explicitly notes that Comic (Flexible) cannot
represent multiple same-named runs separately without user intervention such as
attaching the year to the series name, while retaining flexible Volume, TPB and
Special grouping.

The stricter Comic model uses a different contract: when both `Series` and
`Volume` are present it constructs `Series (Volume)`, with the volume expected to
identify the run (normally its starting year), and it follows ComicVine-style
handling where trades and annuals are separate entities. Supporting that model
would therefore require a deliberate alternate projection mode rather than a
silent metadata rewrite. It remains a deliberate future projection mode rather than a silent metadata rewrite.

Existing Comic (Flexible) libraries and already-published comics using this contract should
not be migrated or retagged merely to adopt the stricter Comic model.

Kavita does not require an Author directory layer for books. Its scanner uses
internal metadata and filenames and requires each book/series to live below the
library root, so the default `Title/Title.ext` and `Series/...` layouts are
intentional. See Kavita's current [file-structure guidance](https://wiki.kavitareader.com/guides/scanner/managefiles/)
and [EPUB scanner guidance](https://wiki.kavitareader.com/guides/scanner/epub/).

Kavita Ingest does not download media, manipulate Kavita's database, watch
directories as a daemon, silently accept matches, provide a GUI, or reconstruct
a deleted original through rollback.

## Requirements

- Linux and Python 3.12 or newer.
- A destination filesystem supporting hard links.
- Calibre 9.12.0 or newer, including `ebook-meta`, for EPUB and PDF metadata
  writes. Kavita Ingest enforces this floor and disables Calibre Python templates
  for its metadata subprocesses.
- `unrar` for CBR/RAR inspection and repacking.

On Linux, install Calibre using its current official binary distribution rather
than relying on a distribution package that may be outdated. `kavita-ingest
doctor` reports the installed version and blocks EPUB/PDF metadata writes when
the safe floor is not met. Docker is not required.

## Installation

For an isolated command-line installation, use `pipx` from a local clone:

```bash
sudo apt install unrar pipx
# Install the current official Calibre Linux binary from:
# https://calibre-ebook.com/download_linux
ebook-meta --version
pipx install '/path/to/kavita-ingest'
kavita-ingest --version
kavita-ingest doctor
```

For development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,compatibility-test]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy --strict src
```

The package exposes the `kavita-ingest` console script. The current supported
release is `1.1.0`.

The `compatibility-test` extra is only for rerunning Milestone 0 experiments. It
includes `comicinfoxml`, which those experiments found unsuitable for production
round-trip preservation; production ComicInfo writing uses the hardened `lxml`
implementation included in the normal package.

### Safe source bundles

Do not ZIP the working directory directly: local clones may contain ignored
credentials, Git internals, caches, build output, virtual environments and state.
For a review/source archive, first commit the intended changes and use:

```bash
scripts/create-source-bundle.sh
```

The command requires a clean Git worktree and builds from `git archive`, making
Git's committed `HEAD` the allow-list. The default output is written below
ignored `dist/`; an explicit output path may be supplied as the first argument.
The script validates the ZIP and refuses to publish it if local-only material
such as `.env`, `.git`, caches, build output, SQLite state or logs appears.

## Configuration

Create a commented configuration without embedding provider secrets:

```bash
kavita-ingest init
kavita-ingest doctor
```

`init` refuses to overwrite an existing file unless `--force` is explicitly
given. The default locations follow the operating system's XDG directories:

- Config: `~/.config/kavita-ingest/config.toml`
- State database and rotating log: `~/.local/state/kavita-ingest/`

Core path configuration resembles:

```toml
[paths]
incoming = ["~/Incoming/Reading"]
books = "~/Libraries/Kavita/Books"
comics = "~/Libraries/Kavita/Comics"
staging = "~/Libraries/Kavita/.staging"

[source]
lifecycle = "preserve"
# archive_root = "~/Archives/Reading-Originals"

[cbr]
convert_to_cbz = true

[naming]
book_folder = "{series_or_title}"
book = "{title}"
book_series = "{series} - {number} - {title}"
comic_folder = "{series}"
comic = "{series} - {number} - {title}"
comic_specials_subfolder = true

[sequence]
integer_padding = 3

[permissions]
file_mode = "0644"
directory_mode = "0755"

[providers.comic_vine]
enabled = false
```

Apply always creates its action-specific staging area on the destination
filesystem, regardless of the convenience staging path. Dangerous nesting,
identical Books/Comics roots, invalid thresholds and archive limits, and an
archive lifecycle without an archive root are configuration errors rather than
silently corrected assumptions.

### Providers

Open Library requires no account or API key. A contact identity is recommended
so requests use its identified rate policy. Configure secrets in the process
environment:

```bash
export KAVITA_INGEST_OPEN_LIBRARY_CONTACT='you@example.com'
export GOOGLE_BOOKS_API_KEY='...'
export COMIC_VINE_API_KEY='...'
```

Google Books supports anonymous access with lower practical quota. Comic Vine
network access requires its key. Doctor reports only presence or absence and
never prints values. Provider cache and durable rate-limit state live in SQLite.

## Workflow

For normal interactive use, start with:

```bash
kavita-ingest
# Or select a configuration explicitly:
kavita-ingest --config /path/to/config.toml
```

The state-aware home screen shows configured paths and lifecycle. With one valid
incoming root, Enter begins immediately; source selection can still be changed.
The wizard performs preflight and discovery, presents compact review controls,
offers human metadata and technical immutable-plan views, binds approval to the
exact persisted digest, and asks separately before apply. It finishes with
verified publication and source-lifecycle results.

Compact review has no implicit action: Enter alone does not skip or accept an
item. Before the guided wizard leaves Review, every discovered item needs an
explicit outcome. Accepted/work-only/manual identities are eligible for planning;
explicit rejected, unresolved and skipped items remain recorded but are excluded.
This allows a partial plan for approved work without inventing a decision for the
items deliberately left unresolved. A genuinely missing decision remains
incomplete review.

`kavita-ingest wizard` is the explicit equivalent. Draft plans, approved
unapplied plans, unresolved review, accepted decisions awaiting a plan, and
interrupted apply state are detected from SQLite on restart. Accepted reviewed
state resumes directly into offline planning without repeating provider work.
Recovery-required state takes priority over starting new work. Bare invocation
in a pipe or other non-interactive session prints deterministic help.

With the `preserve` lifecycle, a later wizard run recognizes an unchanged source
only when a durable COMPLETE apply record, source path and SHA-256, and the
current destination SHA-256 all agree. It reports and excludes that source from
ordinary review. Changed source bytes, a missing destination, or a destination
whose bytes no longer match remain visible as current work with an appropriate
warning. Explicit Reprocess returns the item through review, immutable planning,
digest-bound approval, and normal no-overwrite apply checks; it never grants an
overwrite exception.

Every operation remains available as a scriptable subcommand:

```bash
kavita-ingest init
kavita-ingest doctor
kavita-ingest wizard
kavita-ingest scan /path/to/incoming
kavita-ingest audit /path/to/incoming
kavita-ingest review /path/to/incoming
kavita-ingest plan create /path/to/incoming
kavita-ingest plan show 1
kavita-ingest plan approve 1 --digest DISPLAYED_SHA256
kavita-ingest apply 1
kavita-ingest apply-status 1 --details
kavita-ingest recover 1
kavita-ingest abandon 1 --reason "start over after reviewing durable state"
kavita-ingest status
```

`scan` fingerprints and inspects sources without modifying them. `audit` queries
enabled providers or their caches and ranks candidates, but accepts nothing.
`review` records explicit append-only decisions. Manual identity entry covers
book authors/series/edition fields and comic run year, type, sequence, and
collection volume. A high confidence score means
only that a candidate is eligible for convenient review; it never authorizes a
filesystem change.

Comic run selection is separate from issue acceptance. Use `run-group choose`,
`run-group history`, and `run-group clear` to resolve same-named provider runs
without contaminating canonical issue identity.

### Work-only books

Sparse ebook provider records may identify the work reliably without proving a
specific edition. A work-only acceptance owns work fields such as title and
authors while leaving unresolved edition fields, including publisher, date,
language, and identifiers, untouched. Provider `BOOK_WORK` candidates are
intrinsically work-only: ordinary Accept presents an explicit warning and cannot
turn aggregate work data into edition metadata.

ComicInfo projection includes resolved creator roles, publisher, conservative
publication date components, and language. Ordinary comic issues and run context
use Comic Vine. Collected editions are not guessed from same-titled Comic Vine
volumes: edition-capable book providers may supply a true edition record, which
is then adapted into the comic-collection domain. Provider lookup uses a bounded
query ladder for catalogue conventions that embed creator credits in the title
and spell collection numbers as either digits or words, then falls back to a
relaxed series/creator search only when the precise variants find nothing. A
numbered local collection must still have independent provider title/sequence
evidence for the same Book/Volume number before adaptation; the local number is
never silently copied onto a sequence-less or contradictory edition. Regular
issue records still cannot satisfy a collected-edition identity.

PDF comics use a separate Calibre/XMP projection rather than ComicInfo fields.
The canonical issue number remains in the filename for Comic (Flexible) parsing;
`calibre:series_index` is not repurposed as an issue number because Kavita treats
that PDF field as a volume.

Existing ComicInfo is preserved conservatively. Schema-known elements are
emitted in deterministic ComicInfo 2.1 order, including pre-existing fields and
`Pages`. Unknown extensions, attributes, and invalid unowned known values are
never deleted or silently rewritten to force compliance. New comic plans freeze
an explicit ComicInfo compatibility profile. The supported production profile
preserves the commonly encountered unowned `Page@ImageHash` attribute byte-for-
value, validates the document against the pinned 2.1 XSD with only that exact
attribute omitted from a temporary validation copy, and independently verifies
that the published ImageHash sequence is unchanged. Other unknown attributes,
extensions, and invalid known values still stop publication with the exact XSD
diagnostics while the source remains untouched. Plans that predate the explicit
profile must be regenerated and re-approved before comic Apply.

### Plans and approval

SQLite stores the sole authoritative canonical JSON bytes for each immutable
plan. Exports are exact derivatives. A plan snapshot contains the resolved
metadata, Kavita projection, writer requirements, source SHA-256 and detected
content signature, destination, transformation, source inventory, verification
requirements, source lifecycle, naming policy, archive safety limits, and CBR
conversion policy. Apply re-detects that signature instead of trusting a filename
extension, so ZIP-backed comics carrying a `.cbr` suffix can be normalized safely
to `.cbz` without weakening source preconditions.

`plan create ROOT` consumes only persisted scan/classification evidence and the
latest explicit review decisions under `ROOT`. It re-fingerprints every source,
resolves canonical identity from the accepted decision snapshot, projects the
configured Kavita destination, freezes all effective policy, and stores a draft.
It performs no provider lookup. Unapproved, rejected, unresolved, skipped,
stale, unsupported, or conflicting items are reported rather than inferred into
the plan. A plan may therefore be deliberately partial: accepted items can be
included while explicit non-acceptance outcomes remain durable exclusions.

`plan show --summary` displays a human-readable source, identity, destination,
lifecycle, conflict, and policy summary; omit `--summary` for the authoritative
contents and full digest. `plan approve
--digest` explicitly binds approval to those exact bytes. A later identity or
comic run-group decision invalidates affected plans; a newer unapplied plan for
the same source supersedes the older one. Apply has no flag that manufactures
approval or reinterprets the plan using current provider data.

Plans created with planning-policy version 1 remain readable, exportable and
reportable as history, but cannot be newly approved or applied. Those plans did
not freeze publication file/directory modes and must be regenerated with the
current policy rather than silently acquiring `0644`/`0755` semantics later.

`plan import FILE` is the advanced path for an already canonical, schema-valid
plan document. It always creates an unapproved draft and does not replace the
normal scan-review-create workflow.

### Naming defaults

Standalone books default to `Title/Title.ext`. Series books default to
`Series/Series - 001 - Title.ext`. Comics use their projected, year-disambiguated
Series for both grouping and naming. Simple integers are padded to the configured
width; fractional and symbolic values such as `0.5`, `1A`, `1-5`, and `TPB1`
remain meaningful. Missing cosmetic issue titles are omitted cleanly.

## Apply guarantees

Before media mutation, apply checks the entire plan: source fingerprints,
destinations, capabilities, path permissions, format restrictions, lifecycle,
atomic publication support, and aggregate temporary-space estimates.

Each item follows:

```text
source -> destination-filesystem stage -> independent verification
       -> durable VERIFIED journal -> atomic no-clobber publication
       -> destination verification -> durable COMMITTED journal
       -> planned source lifecycle
```

Linux publication uses `link(2)`. It atomically creates a destination name and
fails if that name already exists. A verified stage is immutable: all writer
handles are closed before `VERIFIED`, no writer is invoked again, and recovery
only reads or unlinks a surviving staging hard link. Files, directories, and
SQLite journal transitions have separate durability barriers.

Source policies:

- `move_after_verify`: remove the incoming source only after staged verification,
  atomic destination commit, destination verification, and durable `COMMITTED`.
- `preserve`: leave the original untouched.
- `archive_after_verify`: publish an exact archive copy without overwrite, then
  remove the incoming source.

Whole-plan atomicity is not promised. Safety and recoverability are per item.

New output files default to mode `0644` and newly created library directories to
`0755`. These are quoted octal strings under `[permissions]`; group-writable
`0664`/`0775` are supported, while executable media and world-writable modes are
rejected. The modes are frozen into the immutable plan and applied to staging
before publication, so the published inode has the planned mode. Existing
directories and source modes are not changed.

## Recovery and rollback preview

Use `kavita-ingest apply-status PLAN_ID` for a human summary, add `--details` to
inspect per-item recovery evidence, and use
`kavita-ingest recover PLAN_ID` to resume only transitions proven safe by the
immutable plan, journal, source, stage, destination, and recorded hashes. It
never rematches metadata, queries providers, invents a destination, or repairs a
published file through a surviving hard link.

If a failed/recovery-required run is safely abandonable and you deliberately want
to start over, use `kavita-ingest abandon PLAN_ID` after inspecting
`apply-status PLAN_ID --details`. Abandonment preserves the journal, invalidates
the immutable plan and modifies no media files. It is refused for completed runs
and for states whose filesystem outcome is uncertain. Historical invalidation
remains auditable; the wizard retires its replacement warning only when all
unfinished work from that plan has a later completed replacement.

`kavita-ingest rollback PLAN_ID` is preview-only. It may identify a reversible
case when an unchanged destination and a proven preserved/archived original
exist. It refuses changed destinations and explains when `move_after_verify`
deleted the only original. Transformed EPUB/PDF/CBZ output cannot recreate an
original CBR or original metadata bytes.

Normal `kavita-ingest status` reports the last ingest, recovery state, reviewed
items, active draft/approved plans and the next useful action. Raw table counters
remain available with `status --metrics`; additional plan-state counts use
`status --details`, and stable machine output remains available with `--json`.

## JSON and logging

`doctor`, `scan`, `audit`, `status`, `plan show`, `apply`, `apply-status`, and
`recover` provide `--json`. Their top-level object includes `output_version` and
`command`. JSON never includes provider credentials or environment values.

Normal operations write a rotating human-readable log beside the state database.
Known provider values, authorization headers, and secret query parameters are
redacted. Provider payload/evidence detail remains a debug concern.

## Limitations

- Apply/recovery/no-clobber guarantees currently target Linux hard-link filesystems.
- Multi-volume and encrypted RAR/CBR inputs are unsupported.
- Encrypted and signature-bearing PDFs cannot be metadata-written.
- Symbolic/range ComicInfo numbering has parser/schema tests but incomplete live
  Kavita coverage.
- Collected-edition identity depends on true edition records from Google Books or
  Open Library; sparse provider coverage can still require explicit manual identity.
- Live Kavita behavior can still reveal personal naming/presentation preferences.
- Rollback is conservative preview only.

See [CONTROLLED_USE.md](CONTROLLED_USE.md) before the first real ingestion. Begin
with one CBZ and one EPUB using `preserve`; do not begin routine
`move_after_verify` operation until their filesystem output and Kavita display
have been reviewed.
