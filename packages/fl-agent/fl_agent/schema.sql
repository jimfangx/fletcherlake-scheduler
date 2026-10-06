-- Schema version 1. Keep changes as ordered migrations, never destructive bootstrap SQL.
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    board_id TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS board_queue (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    board_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS board_queue_board ON board_queue(board_id);
CREATE TABLE IF NOT EXISTS job_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(job_id),
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    board_id TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    expires_at TEXT,
    deleted_at TEXT,
    PRIMARY KEY(job_id, kind)
);
CREATE INDEX IF NOT EXISTS artifact_expiry ON artifacts(expires_at);
CREATE TABLE IF NOT EXISTS collateral_deletion (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    state TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS board_state (
    board_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    active_job TEXT REFERENCES jobs(job_id),
    error TEXT
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_commands (
    message_id TEXT PRIMARY KEY,
    response TEXT NOT NULL
);
PRAGMA user_version = 1;
