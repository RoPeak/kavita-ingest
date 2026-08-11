CREATE TABLE plan_preconditions (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    decision_head_id INTEGER NOT NULL REFERENCES decisions(id),
    run_group_key TEXT,
    run_group_decision_id INTEGER REFERENCES run_group_decisions(id),
    PRIMARY KEY(plan_id, item_id),
    FOREIGN KEY(plan_id, item_id) REFERENCES plan_items_index(plan_id, item_id)
);

CREATE INDEX plan_preconditions_source_idx
    ON plan_preconditions(source_fingerprint, decision_head_id);

CREATE INDEX plan_preconditions_run_group_idx
    ON plan_preconditions(run_group_key, run_group_decision_id);
