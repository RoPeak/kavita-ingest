# First Controlled Ingestion

Do this only after the current implementation has been reviewed and approved.
The first trial should use one confidently identified CBZ and one EPUB. Keep
both originals with the `preserve` lifecycle.

## Before applying

- [ ] Back up the Kavita library and Kavita Ingest state database.
- [ ] Run `kavita-ingest doctor`; resolve every relevant `BLOCKED` result.
- [ ] Confirm Books and Comics destination roots point at the intended libraries.
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
kavita-ingest wizard
```

It still requires an explicit identity decision, exact-plan approval, and a
separate apply confirmation. Quit after plan creation for a plan-only review,
then rerun the wizard to resume the saved draft or approved plan.

The equivalent granular path remains available:

```bash
kavita-ingest plan create /path/to/trial-incoming
kavita-ingest plan show PLAN_ID
kavita-ingest plan approve PLAN_ID --digest DISPLAYED_SHA256
kavita-ingest apply PLAN_ID
kavita-ingest apply-status PLAN_ID
```

Use `apply-status PLAN_ID --details` when diagnosing an interrupted item. If the
wizard reports recovery-required state, review its durable status before
confirming recovery; do not start a new plan to bypass that state.

- [ ] Confirm both original incoming files still exist unchanged.
- [ ] Open the resulting EPUB and CBZ independently.
- [ ] Inspect EPUB metadata and CBZ `ComicInfo.xml`.
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
