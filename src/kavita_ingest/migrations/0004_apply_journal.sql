CREATE TABLE plan_invalidations (
    plan_id INTEGER PRIMARY KEY REFERENCES plans(id),
    reason TEXT NOT NULL,
    invalidated_at TEXT NOT NULL
);

CREATE TABLE plan_supersessions (
    old_plan_id INTEGER PRIMARY KEY REFERENCES plans(id),
    new_plan_id INTEGER NOT NULL REFERENCES plans(id),
    superseded_at TEXT NOT NULL,
    CHECK(old_plan_id <> new_plan_id)
);

CREATE TABLE apply_runs (
    id TEXT PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    plan_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'preflighting', 'running', 'recovery_required', 'failed', 'complete'
    )),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE UNIQUE INDEX apply_runs_one_active_plan
    ON apply_runs(plan_id)
    WHERE status IN ('preflighting', 'running', 'recovery_required');

CREATE TABLE apply_items (
    run_id TEXT NOT NULL REFERENCES apply_runs(id) ON DELETE RESTRICT,
    item_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'preflight_ok', 'staging', 'staged', 'verified',
        'committing', 'committed', 'cleanup_pending', 'cleaned', 'complete',
        'failed', 'stale', 'recovery_required'
    )),
    source_path TEXT NOT NULL,
    planned_source_hash TEXT NOT NULL,
    staging_path TEXT,
    staged_hash TEXT,
    staged_size INTEGER,
    verification_json TEXT,
    destination_path TEXT NOT NULL,
    destination_hash TEXT,
    lifecycle_policy TEXT NOT NULL,
    archive_path TEXT,
    cleanup_path TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    recovery_detail TEXT,
    PRIMARY KEY(run_id, item_id)
);

CREATE INDEX apply_items_state_idx ON apply_items(run_id, state);

CREATE TABLE apply_journal_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    item_id TEXT,
    from_state TEXT,
    to_state TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES apply_runs(id) ON DELETE RESTRICT
);

CREATE INDEX apply_events_run_idx ON apply_journal_events(run_id, id);

CREATE TRIGGER apply_runs_identity_immutable
BEFORE UPDATE OF id, plan_id, plan_digest, started_at ON apply_runs
BEGIN
    SELECT RAISE(ABORT, 'apply run identity is immutable');
END;

CREATE TRIGGER apply_items_identity_immutable
BEFORE UPDATE OF run_id, item_id, source_path, planned_source_hash,
                 destination_path, lifecycle_policy, archive_path, started_at
ON apply_items
BEGIN
    SELECT RAISE(ABORT, 'apply item identity is immutable');
END;
