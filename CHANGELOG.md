# Changelog

All notable release-level changes to Kavita Ingest are recorded here.

## [Unreleased]

## [1.1.0] - 2026-08-14

### Fixed

- Bind new immutable plans to the detected source content signature and re-check it at Apply, replacing brittle filename-suffix trust and safely supporting archive/container normalization to CBZ.
- Route comic PDFs through PDF-safe Calibre/XMP projection instead of sending ComicInfo fields and clear operations to the PDF writer.
- Resolve collected editions through true book-edition records from edition-capable providers rather than guessing collection semantics from Comic Vine volumes; standalone collections no longer require an invented sequence number.
- Replace issue-derived group-run selection with explicit Comic Vine run selection that requires a known start year, append-only invalidates incompatible prior issue decisions, and refreshes wizard review after the run changes.
- Natural-sort numbered incoming filenames so issue runs review as `#1`, `#2`, ... `#10` rather than lexical `#1`, `#10`, ... `#2`.
- Refuse normal acceptance of issue candidates whose Comic Vine run start year is unresolved and surface the group-run chooser directly in the wizard.
- Use Open Library's documented edition-aware search response for collected editions, reject issue-shaped book results, preserve embedded ComicInfo creator/publisher evidence, and present edition date/provider/ISBN evidence during review.
- Parse explicit `by Creator and Creator` credits from long unnumbered collected-edition filenames without treating ordinary one-word `by ...` titles as creator credits.
- Add live wizard Apply progress, compact batch hydration output, natural plan/result display order, and deliberate pagination pauses for large interactive plans.

### Safety

- Plans created before content-signature preconditions remain readable and abandonable for audit/recovery closure, but must be recreated and explicitly re-approved before Apply or Recover under the new semantics.

## [1.0.0] - 2026-08-14

First supported v1 release.

### Added

- Safety-focused ingest pipeline for EPUB books, PDF books, CBZ comics and ordinary single-volume CBR archives.
- Provider-backed identification using Open Library, Google Books and Comic Vine with explicit review authorization before filesystem actions.
- Immutable canonical plans, digest-bound approval, durable apply journalling and source precondition checks.
- Recovery and safe abandonment workflows that preserve journal evidence and refuse uncertain filesystem states.
- Partial planning for explicitly accepted work while rejected, skipped or unresolved items remain durable exclusions.
- Safe source-bundle tooling based on Git's committed-file allow-list.

### Changed

- PDF metadata writing now uses safe Calibre XMP on staged copies and independently verifies page-tree, content-stream and resource semantics plus owned XMP fields.
- EPUB metadata writing requires safe Calibre and preserves non-OPF publication resources while verifying unowned OPF metadata.
- Resume detection is decision-head aware, avoiding accidental consumption of fresh review decisions for identical source bytes.
- Historical planning noise and resolved invalidation notices are handled conservatively without deleting audit history.
- Kavita `Comic (Flexible)` is the deliberate v1 comic-library target; same-named runs encode the run year in projected `Series`.

### Security and safety

- Calibre 9.12.0 or newer is required for EPUB/PDF metadata processing.
- Calibre Python templates are explicitly disabled for metadata subprocesses.
- Destination publication is no-clobber and staged beside the destination for atomic publication.
- Apply does not rematch provider metadata or reinterpret approved identities.
- Encrypted and signature-bearing PDFs are not metadata-write candidates.
- Encrypted, linked or multi-volume CBR/RAR sets are refused where the safety contract is unsupported.
- CBR-to-CBZ conversion verifies publication payload preservation.

### Known limitations

- The v1 comic projection targets Kavita `Comic (Flexible)`, not the stricter `Comic` library model.
- PDF metadata clearing is not part of the v1 write contract.
- Multi-volume RAR sets are not supported.
- `rollback` remains preview-oriented; recovery and abandonment are the supported interrupted-apply workflows.
- The disposable live-Kavita database compatibility test is opt-in and requires `KAVITA_LIVE_DB`; the normal automated release suite reports it as skipped when that disposable database is unavailable.
