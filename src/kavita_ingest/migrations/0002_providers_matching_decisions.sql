CREATE TABLE provider_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX provider_cache_expiry_idx ON provider_cache(provider, expires_at);

CREATE TABLE provider_rate_reservations (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    bucket TEXT NOT NULL,
    reserved_at REAL NOT NULL
);

CREATE INDEX provider_rate_window_idx
    ON provider_rate_reservations(provider, bucket, reserved_at);

CREATE TABLE provider_blocks (
    provider TEXT NOT NULL,
    bucket TEXT NOT NULL,
    blocked_until REAL NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(provider, bucket)
);

CREATE TABLE match_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    mode TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE match_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES match_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    candidate_key TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    score_json TEXT NOT NULL,
    source_evidence_hash TEXT NOT NULL,
    candidate_data_hash TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, source_id, candidate_key)
);

CREATE INDEX match_candidates_source_idx
    ON match_candidates(source_id, run_id, rank);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    media_signature TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    candidate_key TEXT,
    source_evidence_hash TEXT NOT NULL,
    candidate_data_hash TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES decisions(id),
    batch_id TEXT
);

CREATE INDEX decisions_source_idx
    ON decisions(source_fingerprint, media_signature, created_at DESC);
CREATE INDEX decisions_candidate_idx
    ON decisions(source_fingerprint, candidate_key, created_at DESC);
