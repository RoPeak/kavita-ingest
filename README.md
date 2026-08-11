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
| PDF | Yes | PDF metadata | Unsigned, unencrypted PDFs only |
| CBZ | Yes | CBZ + ComicInfo 2.1 | Existing publication payloads preserved |
| CBR/RAR3 | Yes | Repacked CBZ + ComicInfo | Single-volume ordinary archives |
| CBR/RAR5 | Yes | Repacked CBZ + ComicInfo | Single-volume ordinary archives |

Book output targets a Kavita **Books** library. Comics target Kavita **Comic
(Flexible)**; projected ComicInfo `Series` includes a run-start year when needed
to distinguish same-named runs.

Kavita Ingest does not download media, manipulate Kavita's database, watch
directories as a daemon, silently accept matches, provide a GUI, or reconstruct
a deleted original through rollback.

## Requirements

- Linux and Python 3.12 or newer.
- A destination filesystem supporting hard links.
- Calibre's `ebook-meta` for EPUB metadata writes.
- `unrar` for CBR/RAR inspection and repacking.

Milestone 0 validated Calibre/`ebook-meta` 7.6.0 and `unrar` 7.0.7. These are
known-good versions, not claimed universal minimum versions. `kavita-ingest
doctor` reports the actual local capabilities before use. Docker is not
required.

## Installation

For an isolated command-line installation, use `pipx` from a local clone:

```bash
sudo apt install calibre unrar pipx
pipx install '/path/to/kavita-ingest'
kavita-ingest --version
```

For development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,compatibility-test]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy --strict src
```

The package exposes the `kavita-ingest` console script and is currently version
`0.1.0`, a pre-1.0 MVP.

The `compatibility-test` extra is only for rerunning Milestone 0 experiments. It
includes `comicinfoxml`, which those experiments found unsuitable for production
round-trip preservation; production ComicInfo writing uses the hardened `lxml`
implementation included in the normal package.

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

Running `kavita-ingest` without arguments opens a small numbered workflow menu.
Every operation also remains available as a scriptable subcommand:

```bash
kavita-ingest init
kavita-ingest doctor
kavita-ingest scan /path/to/incoming
kavita-ingest audit /path/to/incoming
kavita-ingest review /path/to/incoming
kavita-ingest plan create /path/to/incoming
kavita-ingest plan show 1
kavita-ingest plan approve 1 --digest DISPLAYED_SHA256
kavita-ingest apply 1
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
language, and identifiers, untouched.

### Plans and approval

SQLite stores the sole authoritative canonical JSON bytes for each immutable
plan. Exports are exact derivatives. A plan snapshot contains the resolved
metadata, Kavita projection, writer requirements, source SHA-256, destination,
transformation, source inventory, verification requirements, source lifecycle,
naming policy, archive safety limits, and CBR conversion policy.

`plan create ROOT` consumes only persisted scan/classification evidence and the
latest explicit review decisions under `ROOT`. It re-fingerprints every source,
resolves canonical identity from the accepted decision snapshot, projects the
configured Kavita destination, freezes all effective policy, and stores a draft.
It performs no provider lookup. Unapproved, rejected, unresolved, skipped,
stale, unsupported, or conflicting items are reported rather than inferred into
the plan.

`plan show` displays the digest and authoritative contents. `plan approve
--digest` explicitly binds approval to those exact bytes. A later identity or
comic run-group decision invalidates affected plans; a newer unapplied plan for
the same source supersedes the older one. Apply has no flag that manufactures
approval or reinterprets the plan using current provider data.

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

## Recovery and rollback preview

Use `kavita-ingest apply-status PLAN_ID` to inspect an interrupted run and
`kavita-ingest recover PLAN_ID` to resume only transitions proven safe by the
immutable plan, journal, source, stage, destination, and recorded hashes. It
never rematches metadata, queries providers, invents a destination, or repairs a
published file through a surviving hard link.

`kavita-ingest rollback PLAN_ID` is preview-only. It may identify a reversible
case when an unchanged destination and a proven preserved/archived original
exist. It refuses changed destinations and explains when `move_after_verify`
deleted the only original. Transformed EPUB/PDF/CBZ output cannot recreate an
original CBR or original metadata bytes.

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
- Comic Vine collected-edition coverage is incomplete and ebook edition metadata
  can be sparse.
- Live Kavita behavior can still reveal personal naming/presentation preferences.
- Rollback is conservative preview only.

See [CONTROLLED_USE.md](CONTROLLED_USE.md) before the first real ingestion. Begin
with one CBZ and one EPUB using `preserve`; do not begin routine
`move_after_verify` operation until their filesystem output and Kavita display
have been reviewed.
