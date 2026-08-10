# Milestones 4-5 Implementation Report

## Scope and safety

This pass adds provider-backed identification, deterministic matching, persisted
review evidence, and explicit user decisions. It does not contain metadata
writers, archive repacking, destination planning, filesystem plans, apply,
move/delete, or rollback functionality. Auditing and review never modify media.

The existing database migration invariant remains in force: before applying a
new numbered migration, kavita-ingest creates a timestamped backup and verifies
its SQLite integrity. The v1-to-v2 path is covered by a regression test.

## Database migration

`0002_providers_matching_decisions.sql` adds:

- `provider_cache`: raw and normalized provider evidence, request identity,
  schema version, provenance hashes, fetch time, and expiry.
- `provider_rate_reservations`: durable pre-request reservations used for
  rolling-window and minimum-interval enforcement.
- `provider_blocks`: provider/resource blocks such as `Retry-After` responses.
- `match_runs`: immutable audit-run summaries.
- `match_candidates`: ranked candidate scores, comparisons, contradictions,
  reconciliation, and explanations tied to source fingerprints.
- `decisions`: append-only accepted, work-only, rejected, unresolved, skipped,
  manual-identity, and manual-field decisions with supersession links.

Provider cache data remains evidence rather than canonical truth. Decisions are
bound to content SHA-256 plus stable media characteristics rather than paths.

## Provider infrastructure

All adapters return application-owned `NormalizedCandidate` records.

| Provider | Implemented operations | Live state in this environment |
| --- | --- | --- |
| Open Library | ISBN edition lookup, structured work search, work/edition fetch | Available in unidentified mode; live responses were unusually slow |
| Google Books | ISBN lookup, structured volume search, volume fetch | Implemented and fixture-tested; anonymous live requests returned HTTP 429/quota 0 |
| Comic Vine | run, issue, and collection-oriented search/fetch | Implemented and fixture-tested; live access disabled because no API key is configured |

Open Library sends the configured contact in its User-Agent when present. Its
unidentified default is 0.8 requests/second; identified mode defaults to 2.5.
Comic Vine uses resource-aware buckets, 180 reservations per rolling hour by
default, and a 1.25 second minimum interval. Reservations occur before network
calls and retries consume quota. HTTP 429 creates a durable block and is not
blindly retried. Cached Comic Vine data remains usable without a current key.

The environment did not provide `KAVITA_INGEST_OPEN_LIBRARY_CONTACT`,
`GOOGLE_BOOKS_API_KEY`, or `COMIC_VINE_API_KEY`. No credentials were fabricated.

## Matching and decisions

Candidate generation progressively weakens queries: exact provider identifiers,
local identifiers, structured identity, then relaxed searches. Book providers
are not queried for comics, Comic Vine is not queried for books, and collected
editions use collection-oriented queries rather than issue matching.

Scores retain field comparisons, confidence, provenance, positive/negative
components, contradictions, runner-up margin, and an explanation. Default
eligibility requires score 92, margin 12, classification confidence 0.90,
identity-field confidence, and no hard contradiction. Eligibility never creates
an approval.

Book work and edition resolution are independent. A work may be accepted while
its edition remains unresolved. The Rich/Typer review loop supports individual
acceptance, work-only acceptance, exact-count batch acceptance, rejection,
re-search, typed manual fields/identity, unresolved, skipped, and detailed
explanation. Manual values have `user` provenance and remain sticky until
explicitly cleared. Superseded decisions remain in the audit history.

## Real-library audit

The media tree `/home/ronan/Downloads/Torrented-Media` was read only. The audit
database and provider cache were stored under `/tmp`; no real decision was
accepted and no media bytes or metadata were changed.

An offline replay using the live Open Library cache completed over all 188
sources:

| Result | Count |
| --- | ---: |
| Sources | 188 |
| Candidate found | 1 |
| No candidate | 187 |
| Eligible high-confidence | 0 |
| Review required | 1 |
| Unresolved | 187 |
| Provider unavailable | 188 |
| Work accepted / edition unresolved | 1 |
| Hard classification/provider contradictions | 0 |
| Collected editions kept out of issue matching | 5 |
| Collected editions incorrectly treated as issues | 0 |

`provider unavailable` is counted per source when one or more relevant providers
cannot answer and does not mean local inspection failed. The sparse result is
expected: Comic Vine had no key, Google Books was blocked by anonymous quota,
and only six Open Library live-query results were cached. A fresh five-book live
audit populated those Open Library cache entries but did not finish within the
bounded validation session because external responses remained slow; it made no
decisions and left its run incomplete rather than claiming success.

Representative local identities and matching observations:

| Example | Result |
| --- | --- |
| `Crime and Punishment.epub` | Open Library work candidate scored 90; work accepted by reconciliation, exact edition unresolved; not eligible or user-approved |
| `The Odyssey by Homer.epub` | Correct standalone-book identity; no cached/provider candidate |
| `CLI Handbook Flavio Copes.epub` | Correct standalone-book identity; no cached/provider candidate |
| `The Official Raspberry Pi Handbook 2023.pdf` | Correct standalone-book identity; no cached/provider candidate |
| `Absolute Batman 014 ...cbz` | Comic issue, series `Absolute Batman`, sequence `14`; Comic Vine unavailable |
| `Absolute Martian Manhunter 001 ...` | Comic issue, sequence `1`; Comic Vine unavailable |
| `Animal Man by Grant Morrison Book 01 ...cbr` | Collected edition, series `Animal Man`; not sent through issue matching |
| `New X-Men ... Ultimate Collection Book 1 ...` | Collected edition, series `New X-Men`; not sent through issue matching |
| `What If ... 024 ...pdf` | Comic issue, sequence `24` |
| `What If ... 034 -The Watcher...` | Existing spacing regression remains fixed; comic issue, sequence `34` |
| `Watchmen/Watchmen #1.pdf` | Comic issue, sequence `1` |
| `Doomsday Clock #12.pdf` | Comic issue, sequence `12` |
| `Superman - The Kryptonite Spectrum ...` | One-shot classification retained |

External matching exposed one local-analysis problem: embedded ComicInfo `Series`
sometimes contained a collected-edition marketing title. Strongly structured
collection filenames now take precedence for canonical series parsing while the
embedded value remains evidence. Regression tests cover Animal Man and New X-Men.

## Verification

The complete pre-publication suite completed with 136 passed and 1 skipped in
16.30 seconds; the skip is the explicitly opt-in disposable Kavita integration
test. The focused provider/matching/review demonstration completed with 29
passed. `ruff check src tests` was clean, and `mypy --strict src` reported no
issues in 36 source files. Tests use synthetic provider responses and do not
require live APIs.

No Milestone 6 code or media-mutation capability was created.

## Known constraints

- Live Comic Vine validation requires `COMIC_VINE_API_KEY`.
- Useful Open Library throughput should use a configured contact identity.
- This environment's anonymous Google Books allocation reports a daily limit of
  zero; an API key or working project quota is needed for live validation.
- Network timeout bounds apply per request/attempt, so a multi-source live audit
  can still take several minutes when a provider is slow. Offline cached audits
  are deterministic and fast.
- Work acceptance in reconciliation is an analytical state, not authorization.
  Only an explicit persisted decision can approve canonical identity.
