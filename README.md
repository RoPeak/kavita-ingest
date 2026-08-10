# Kavita Ingest

Kavita Ingest currently provides read-only discovery, inspection, and local
classification for EPUB, PDF, CBZ, and ordinary single-file RAR3/RAR5 CBR
sources. It does not yet query metadata providers, write metadata, construct
approved plans, or move files.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,compatibility]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

```bash
.venv/bin/kavita-ingest doctor
.venv/bin/kavita-ingest scan /path/to/incoming --no-persist
```

Omit `--no-persist` to retain source fingerprints, inspection outcomes, and
classification hypotheses in the state database. Scanning never modifies a
source file.

## Configuration

The default configuration is
`~/.config/kavita-ingest/config.toml`; state defaults to
`~/.local/state/kavita-ingest/` on Linux. All path values are optional.

```toml
[paths]
incoming = ["~/Incoming/Reading"]
books = "/srv/kavita/books"
comics = "/srv/kavita/comics"
staging = "/srv/kavita/.staging"
ignore = ["~/Incoming/Reading/ignore"]
# database = "~/.local/state/kavita-ingest/state.sqlite3"

[archive]
max_entries = 5000
max_entry_bytes = 536870912
max_total_bytes = 4294967296
max_path_depth = 20
max_ratio = 1000.0

[logging]
level = "INFO"
```

Destination, staging, and ignored roots are excluded during discovery. Before
an existing SQLite database receives any pending schema migration, Kavita
Ingest creates and integrity-checks a timestamped SQLite backup.

Multi-volume RAR sets are detected and left untouched. For this MVP, one
logical comic archive must be represented by one physical CBR/RAR file.
