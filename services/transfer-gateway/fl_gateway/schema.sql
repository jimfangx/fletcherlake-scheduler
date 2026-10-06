PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL UNIQUE,
    grant_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'OPEN'
);
CREATE TABLE IF NOT EXISTS received_files (
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY(transfer_id, kind)
);
