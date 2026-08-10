# Milestone 0 compatibility findings

Date: 2026-08-10

## Scope and result

This spike validates only external file/tool boundaries and the Kavita Comic
(Flexible) projection. It contains no application CLI, scanner, database,
provider, matcher, planner, or apply engine.

| Integration | Status | Production decision |
| --- | --- | --- |
| `comicinfoxml` | **DESIGN CHANGE** | Do not depend on the package; use a narrow, schema-ordered hardened `lxml` patcher. |
| Calibre `ebook-meta` | **GREEN WITH CONSTRAINTS** | Use for verified supported fields, but not exact publication dates or contributor-role ownership. |
| Narrow EPUB OPF patching | **GREEN** | Own explicit fields/roles only and independently read back the result. |
| `rarfile` + `unrar` | **GREEN WITH CONSTRAINTS** | Support ordinary RAR3/RAR5 and complete multi-volume sets after strict inventory preflight. |
| `pikepdf` | **GREEN WITH CONSTRAINTS** | Support ordinary unsigned/unencrypted PDFs; block encrypted and signature-bearing PDFs. |
| Kavita Comic (Flexible) projection | **GREEN WITH CONSTRAINTS** | Year-disambiguated Series and arbitrary issue Numbers work; Format-marked items become specials. |

## Environment

- Python 3.12.3
- Calibre / `ebook-meta` 7.6.0
- `unrar` 7.00 (`unrar` Ubuntu package 7.0.7)
- `comicinfoxml` 0.5.1
- `comicapi` 3.2.0 and ComicTagger 1.5.5, installed only to investigate the broken plugin boundary
- `lxml` 6.1.1
- `rarfile` 4.5
- `pikepdf` 10.11.0
- pytest 9.1.1
- Kavita 0.9.0.2, official GHCR image digest
  `sha256:880a8feff0833e860575f8e08788e4b4f59f8659afd17206566aae88a525130d`

Primary commands:

```bash
.venv/bin/pytest -v
KAVITA_LIVE_DB=/tmp/kavita-m0/config-host/kavita.db \
  .venv/bin/pytest -v tests/compatibility/test_kavita_live.py
```

Result: **50 passed, 1 opt-in live test skipped** in the normal run; the live
test passed separately against the stopped disposable Kavita database.

## ComicInfo

`comicinfoxml` 0.5.1 is a ComicTagger plugin rather than a standalone
ComicInfo API. Its top-level `ComicInfoXml` class operates on ComicTagger
`GenericMetadata` and `Archiver` objects; byte conversion methods are private.

More importantly, its wheel declares no `comicapi` dependency and fails to
import in isolation. Installing current PyPI `comicapi` 3.2.0 does not repair
it: the plugin imports a `FileHash` symbol absent from that release. Installing
ComicTagger 1.5.5 also does not supply a compatible API and introduces a PyICU
build dependency.

The narrow `lxml` alternative successfully:

- parsed and modified all required Series/Number/Volume/Format/title/date/
  creator/publisher/language/GTIN fields;
- preserved standard unowned fields and an unknown extension element;
- removed explicitly cleared owned fields;
- inserted new elements in ComicInfo 2.1 schema order; and
- serialized a document validated by the official Anansi draft 2.1 XSD.

**Plan amendment:** remove `comicinfoxml` from production dependencies. Wrap a
small schema-aware `lxml` reader/patcher behind the planned ComicInfo interface.
Preserve unknown fields, reject duplicate owned fields after reconciliation,
and validate produced XML against the bundled/pinned schema.

## EPUB and Calibre

### Capability matrix

| Field | Calibre 7.6 result | Production owner |
| --- | --- | --- |
| Title | Exact read-back | `ebook-meta` |
| Authors | Exact read-back; translator/editor/illustrator refinements preserved | `ebook-meta` |
| Publisher | Exact read-back | `ebook-meta` |
| Language | Exact read-back | `ebook-meta` |
| ISBN | Written and independently found in OPF identifiers | `ebook-meta` |
| Additional identifiers | Written and independently found | `ebook-meta`, then normalize/read back |
| Description | Exact read-back | `ebook-meta` |
| Subjects/tags | Multiple subjects written | `ebook-meta` |
| Series/index | Fractional `2.5` preserved using EPUB 3 collection refinements | `ebook-meta` |
| Publication date | **Not exact**: `2025-06-07` became `2025-06-06T23:00:00+00:00` under BST | Narrow OPF patcher |
| Contributor roles | Existing refinements preserved, but no adequate role-specific CLI | Narrow OPF patcher |

Calibre reorders manifest and metadata elements and normalizes identifiers and
dates. It preserved manifest membership, spine references, package identity,
cover reference, custom metadata, creator-role refinements, and every
non-metadata publication resource byte-for-byte.

The narrow OPF patcher added/updated selected `trl`, `edt`, and `ill` roles,
preserved stable creator IDs for unchanged people, retained unowned author and
editor fields, and left all publication resources unchanged.

Production ownership rules:

1. `ebook-meta` runs first for fields in the capability table.
2. The OPF patcher owns exact edition date and only contributor roles listed in
   the approved plan.
3. Existing `(normalized person name, role)` matches reuse creator IDs.
4. For an owned role, stale role refinements are removed; a creator node is
   removed only if no other refinement references it.
5. Omitted roles are unowned and preserved.
6. Package identifier, unknown metadata, manifest, spine, navigation, cover,
   and publication resources are never writer-owned.
7. Every intended field and role is independently read back; helper success is
   insufficient.

**Plan amendment:** move exact edition-date writing from Calibre to the narrow
OPF patcher. Accept both legacy Calibre series tags and EPUB 3
`belongs-to-collection`/`group-position` during inspection.

## RAR and CBR

`rarfile` selected the `unrar` command backend. The fixtures confirmed:

- ordered inventory and payload-byte preservation for ordinary RAR3 and RAR5;
- successful reads from complete RAR3 and RAR5 multi-volume sets;
- symlink detection before writes, including an upstream chained traversal fixture;
- header-encrypted RAR5 detection via `needs_password()` with an empty inventory;
- unsupported/non-RAR input reported as `NotRarFile`;
- missing old-style volume parts surface as `FileNotFoundError`; and
- duplicate and case-colliding names can be rejected by application preflight.

An entry with `compress_size == 0` is not alone proof of a malicious ratio:
RAR links and internal duplicate-data records may report zero compressed size.
Link classification must occur first, and zero compressed size should disable
ratio calculation rather than automatically reject an otherwise regular entry.

Production preflight must cap entry count, per-entry size, aggregate
uncompressed size, path depth, and nonzero compression ratio. It must reject
absolute/traversal paths, links, duplicate names, case-fold collisions,
encrypted archives without an explicit future policy, incomplete volume sets,
and extraction backends other than a diagnosed supported backend. Extraction
must stream regular members individually after full inventory validation.

Fixtures are unchanged files from the ISC-licensed `rarfile` 4.5 test suite;
their provenance and license are included.

## PDF and pikepdf

For an ordinary generated PDF, `pikepdf` wrote title, author, subject,
keywords, language, identifier, and date metadata. The whole-file hash changed,
as expected, while the following remained identical after reopen:

- page count;
- media boxes;
- decoded page content-stream SHA-256 values; and
- decoded referenced image/resource SHA-256 values.

An encrypted fixture required a password to open and was blocked by the writer.
A synthetic signature field was reliably detected and blocked. This proves
signature-field detection, not cryptographic signature validity.

**Production policy:** only write ordinary unencrypted PDFs with no signature
fields. Verify semantic page/resource fingerprints rather than PDF bytes or
object numbers. Do not promise support for encrypted or signed PDFs in MVP.

## Kavita Comic (Flexible)

The disposable Kavita scan confirmed:

- `Absolute Batman (2024)` and `Absolute Batman (2031)` became distinct series;
- canonical `series_title` remained `Absolute Batman` in the fixture model;
- regular issue `1` and fractional issue `70.5` remained ordinary chapters;
- run-start year was not treated as `ComicInfo.Volume`;
- annual, special, one-shot, trade, symbolic trade, omnibus, and graphic novel
  fixtures were ingested successfully; and
- the standalone graphic novel became its own series.

All files carrying a special-like `Format` were represented by Kavita as
specials (`IsSpecial = 1`, special volume sentinel `100000`, chapter sentinel
`-100000`). This included TPB and Omnibus fixtures even when ComicInfo.Volume
was an integer. Their filename stem became Kavita's range/title identity.

**Plan amendment:** retain the canonical-to-projection separation and current
year-disambiguated projected Series. For Format-marked annuals, specials,
trades, omnibuses, and graphic novels:

- encode a unique sequence and title in the filename;
- write Number/Volume when bibliographically meaningful for interoperability;
- do not rely on Number or Volume to control Kavita placement; and
- expect Comic (Flexible) to place them on its Specials representation.

Regular issues may rely on ComicInfo.Number, including fractional values.
Symbolic/range issue values remain supported by the projection model, but only
the tested numeric/fractional values are empirically confirmed in live Kavita.

## Retained safety requirements

- The future application must back up its SQLite state database before applying
  schema migrations.
- Metadata writes remain staged and independently verified.
- Originals remain untouched until verified destination commit.
- No Milestone 1 implementation is included in this repository state.
