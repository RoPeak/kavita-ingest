CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    format TEXT NOT NULL,
    signature TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX sources_sha256_idx ON sources(sha256);

CREATE TABLE inspections (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    inspected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT
);

CREATE INDEX inspections_source_idx ON inspections(source_id, inspected_at DESC);

CREATE TABLE classifications (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    classified_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subtype TEXT NOT NULL,
    confidence REAL NOT NULL,
    ambiguous INTEGER NOT NULL,
    hypotheses_json TEXT NOT NULL
);

CREATE INDEX classifications_source_idx
    ON classifications(source_id, classified_at DESC);
