# Milestone 0 compatibility harness

This directory contains disposable, fixture-driven experiments for external
libraries and file formats considered by `kavita-ingest`. It is intentionally
not an application package and contains no CLI, database, provider, scanner, or
filesystem apply engine.

Run the harness with:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[compatibility]'
CALIBRE_CONFIG_DIRECTORY=/tmp/kavita-ingest-calibre .venv/bin/pytest -v
```

RAR fixtures under `compatibility/fixtures/rar` come from the `rarfile` 4.5
upstream test suite and retain its ISC license and provenance notice.
