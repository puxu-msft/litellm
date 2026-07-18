from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.archive.catalog import (
    Catalog,
    CatalogOpened,
    SegmentRecord,
    SegmentState,
    open_catalog,
)
from litellm.proxy.observability.terminal.identity import SessionAlias, SessionDigest


SEGMENT_ID = UUID("00000000-0000-4000-8000-000000000201")
WORKER_ID = UUID("00000000-0000-4000-8000-000000000202")


def _catalog(path: Path) -> Catalog:
    result = open_catalog(path)
    assert isinstance(result, CatalogOpened)
    return result.catalog


def test_catalog_is_non_rotating_private_wal_and_persists_aliases(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    catalog = _catalog(path)
    digest = SessionDigest(bytes.fromhex("123400" + "00" * 29))
    alias = SessionAlias(digest, digest.display(5))
    catalog.upsert_alias(alias, first_seen_ns=10, last_seen_ns=20)
    catalog.close()

    assert path.stat().st_mode & 0o777 == 0o600
    reopened = _catalog(path)
    assert reopened.load_aliases() == (alias,)
    reopened.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("SELECT COUNT(*) FROM session_aliases").fetchone() == (1,)


def test_segment_publish_state_machine_is_persistent(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    catalog = _catalog(path)
    record = SegmentRecord(
        segment_id=SEGMENT_ID,
        state=SegmentState.ACTIVE,
        path="segments/active.sqlite",
        schema_version=1,
        created_at_ns=100,
    )
    catalog.register_segment(record)
    catalog.transition_segment(SEGMENT_ID, SegmentState.DRAINING)
    catalog.transition_segment(SEGMENT_ID, SegmentState.PUBLISHING, path="segments/final.sqlite")
    catalog.transition_segment(SEGMENT_ID, SegmentState.PUBLISHED)
    assert catalog.get_segment(SEGMENT_ID) == SegmentRecord(
        segment_id=SEGMENT_ID,
        state=SegmentState.PUBLISHED,
        path="segments/final.sqlite",
        schema_version=1,
        created_at_ns=100,
    )
    catalog.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT state,path FROM segments WHERE segment_id=?", (str(SEGMENT_ID),)
        ).fetchone() == (SegmentState.PUBLISHED.value, "segments/final.sqlite")


def test_catalog_rejects_illegal_segment_transition(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path / "catalog.sqlite")
    catalog.register_segment(SegmentRecord(SEGMENT_ID, SegmentState.ACTIVE, "a.sqlite", 1, 100))
    with pytest.raises(ValueError, match="active.*published"):
        catalog.transition_segment(SEGMENT_ID, SegmentState.PUBLISHED)
    assert catalog.get_segment(SEGMENT_ID).state is SegmentState.ACTIVE
    catalog.close()


def test_catalog_binds_request_to_one_owner_segment(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path / "catalog.sqlite")
    request_id = UUID("00000000-0000-4000-8000-000000000203")
    catalog.register_segment(SegmentRecord(SEGMENT_ID, SegmentState.ACTIVE, "a.sqlite", 1, 100))
    assert catalog.bind_request(request_id, SEGMENT_ID, WORKER_ID) is True
    assert catalog.bind_request(request_id, SEGMENT_ID, WORKER_ID) is False
    assert catalog.open_requests(SEGMENT_ID) == ((request_id, WORKER_ID),)
    catalog.mark_request_terminal(request_id, "completed")
    assert catalog.open_requests(SEGMENT_ID) == ()
    catalog.close()
