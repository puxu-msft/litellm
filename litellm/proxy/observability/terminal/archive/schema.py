from __future__ import annotations

SPOOL_SCHEMA_VERSION = 1

SPOOL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spool_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    worker_instance_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spool_events (
    worker_sequence INTEGER PRIMARY KEY CHECK (worker_sequence >= 0),
    event_id TEXT NOT NULL UNIQUE,
    frame BLOB NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
);
"""
