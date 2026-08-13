# First Controlled Ingestion

Do this only after the current implementation has been reviewed and approved.
The first trial should use one confidently identified CBZ and one EPUB. Keep
both originals with the `preserve` lifecycle.

## Before applying

- [ ] Back up the Kavita library and Kavita Ingest state database.
- [ ] Run `kavita-ingest doctor`; resolve every relevant `BLOCKED` result.
- [ ] Confirm Books and Comics destination roots point at the intended libraries.
- [ ] Confirm the Kavita comics library type is **Comic (Flexible)**, not **Comic**.
- [ ] Confirm the source lifecycle is `preserve`.
- [ ] Scan and audit only the two trial files.
- [ ] Explicitly review and accept each identity; do not rely on score alone.
- [ ] Inspect the resolved canonical metadata and Kavita projection.
- [ ] Create the immutable plan and inspect every source, destination and action.
- [ ] Approve the exact displayed plan digest.
- [ ] Confirm no destination path already exists.

## Apply and inspect

The guided path is recommended for normal use:

```bash
kavita-ingest
# With an explicit configuration:
kavita-ingest --config /path/to/config.toml
```

Press Enter on the home screen to use the single configured incoming root. The
wizard still requires an explicit identity decision, offers human metadata and
technical plan inspection, binds approval to the exact persisted plan, and asks
separately before apply. Quit after plan creation for a plan-only review, then
rerun it to resume the saved draft or approved plan. Accepted decisions saved
before plan creation also resume without repeating provider searches.

`kavita-ingest wizard` launches the same guided experience explicitly.

Choose an explicit action for each compact-review item; Enter has no implicit
Next or Accept behavior. Every discovered item needs an explicit review outcome
before the guided wizard leaves Review. Accepted/work-only/manual identities can
enter a partial plan while explicit rejected, unresolved and skipped items remain
saved and excluded. A missing decision is still incomplete review.

When `preserve` leaves a successfully ingested source in incoming, the next run
should report it as already ingested only while both its source hash and verified
destination evidence still match. Use explicit Reprocess for a deliberate new
review. Reprocess does not overwrite an existing destination; change the plan
target or otherwise resolve the destination conflict before approval.

The equivalent granular path remains available:

```bash
kavita-ingest plan create /path/to/trial-incoming
kavita-ingest plan show PLAN_ID
kavita-ingest plan approve PLAN_ID --digest DISPLAYED_SHA256
kavita-ingest apply PLAN_ID
kavita-ingest apply-status PLAN_ID --details
kavita-ingest recover PLAN_ID
kavita-ingest abandon PLAN_ID --reason "start over after reviewing durable state"
```

Use `apply-status PLAN_ID --details` when diagnosing an interrupted item. Prefer
`recover PLAN_ID` when the journal proves a safe continuation. Use `abandon
PLAN_ID` only when you deliberately want to start over and the engine confirms the
run is safely abandonable. Abandonment preserves journal history, invalidates the
old plan and modifies no media; uncertain commit/cleanup states are refused. Do
not create a new plan merely to bypass recovery-required state.

An invalidated or abandoned plan remains auditable history. Its wizard notice is
retired only after all unfinished work has been replaced by a later completed
plan; items that were already complete do not need pointless reprocessing.

EPUB and PDF metadata writes require Calibre 9.12.0 or newer. PDF writes are
performed on staged copies, preserve verified page/content/resource semantics and
independently read back owned XMP fields. Encrypted and signature-bearing PDFs are
not metadata-write candidates.

Historical plans that report planning-policy version 1 must be regenerated
before approval or application. They remain inspectable history, but lack the
immutable publication-permission policy required by current apply safety.

- [ ] Confirm both original incoming files still exist unchanged.
- [ ] Open the resulting EPUB and CBZ independently.
- [ ] Inspect EPUB metadata and CBZ `ComicInfo.xml`.
- [ ] For regular comics, confirm the run year is part of projected `Series` and
      is not being invented as `Volume`.
- [ ] Confirm filenames and folders are personally acceptable.
- [ ] Confirm output files are `0644` (or the configured safe mode) and newly
      created library directories are `0755` (or the configured safe mode).
- [ ] Add or rescan only these outputs in Kavita.
- [ ] Confirm title, author, series, issue number, format and grouping in Kavita.
- [ ] Record any projection or naming changes before processing more media.

## After success

Only after both trial items pass filesystem and Kavita inspection should a small
second batch be attempted. Enable normal `move_after_verify` only when you are
comfortable that the generated outputs are the intended durable copies. This
policy removes an incoming original only after staged verification, atomic
no-clobber destination commit, destination verification, and durable journal
commit.

Rollback remains preview-only. Keep independent backups even after controlled
use is established.
