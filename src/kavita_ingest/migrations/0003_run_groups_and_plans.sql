CREATE TABLE run_group_decisions (
    id INTEGER PRIMARY KEY,
    group_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    provider_run_id TEXT,
    run_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES run_group_decisions(id)
);

CREATE INDEX run_group_decisions_group_idx
    ON run_group_decisions(group_key, provider, id DESC);

CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    canonical_json BLOB NOT NULL,
    byte_length INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approval_digest TEXT,
    CHECK(length(canonical_json) = byte_length),
    CHECK(status = 'draft' OR approval_digest = sha256)
);

CREATE TABLE plan_items_index (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    destination_path TEXT,
    blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
    PRIMARY KEY(plan_id, item_id)
);

CREATE INDEX plan_items_source_idx ON plan_items_index(source_fingerprint);

CREATE TRIGGER plans_payload_immutable
BEFORE UPDATE OF schema_version, canonical_json, byte_length, sha256, created_at ON plans
BEGIN
    SELECT RAISE(ABORT, 'authoritative plan payload is immutable');
END;

CREATE TRIGGER plans_approved_immutable
BEFORE UPDATE ON plans
WHEN OLD.status = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved plan is immutable');
END;

CREATE TRIGGER plans_no_delete
BEFORE DELETE ON plans
BEGIN
    SELECT RAISE(ABORT, 'plans are append-only');
END;
