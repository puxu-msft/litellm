from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.archive.spool import (
    AckCommitted,
    CompactCommitted,
    LoadCommitted,
    OpenFailed,
    Opened,
    SequenceRange,
    SpoolFailureCode,
    StoreCommitted,
    StoreFailed,
    WorkerSpool,
    open_worker_spool,
)
from litellm.proxy.observability.terminal.codec import encode_event_frame
from litellm.proxy.observability.terminal.events import EventEnvelope, EventType
from tests.test_litellm.proxy.observability.terminal.test_events import WORKER_ID, envelope


OTHER_WORKER_ID = UUID("00000000-0000-4000-8000-000000000099")


def _event(sequence: int, *, worker_id: UUID = WORKER_ID) -> EventEnvelope:
    return replace(
        envelope(EventType.REQUEST_ACCEPTED),
        event_id=UUID(int=sequence + 1),
        worker_instance_id=worker_id,
        worker_sequence=sequence,
    )


def _opened(path: Path, worker_id: UUID = WORKER_ID) -> WorkerSpool:
    result = open_worker_spool(path, worker_id)
    assert isinstance(result, Opened)
    return result.spool


def test_open_creates_private_wal_database_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    spool.close()

    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT schema_version, worker_instance_id FROM spool_metadata").fetchone() == (
            1,
            str(WORKER_ID),
        )


def test_reopen_rejects_different_worker_identity(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    _opened(path).close()
    result = open_worker_spool(path, OTHER_WORKER_ID)
    assert isinstance(result, OpenFailed)
    assert result.code is SpoolFailureCode.WORKER_MISMATCH


def test_reopen_rejects_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    _opened(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE spool_metadata SET schema_version=2")
    result = open_worker_spool(path, WORKER_ID)
    assert isinstance(result, OpenFailed)
    assert result.code is SpoolFailureCode.SCHEMA_MISMATCH


def test_store_contiguous_batch_and_read_pending_with_direct_sqlite_oracle(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    events = (_event(0), _event(1), _event(2))

    stored = spool.store(events)
    loaded = spool.load_pending(limit=10)
    spool.close()

    assert stored == StoreCommitted(sequence_range=SequenceRange(0, 2), inserted_count=3)
    assert loaded == LoadCommitted(sequence_range=SequenceRange(0, 2), events=events)
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT worker_sequence, event_id, frame, acknowledged FROM spool_events ORDER BY worker_sequence"
        ).fetchall()
    assert rows == [(event.worker_sequence, str(event.event_id), encode_event_frame(event), 0) for event in events]


def test_exact_duplicate_store_is_idempotent(tmp_path: Path) -> None:
    spool = _opened(tmp_path / "worker.sqlite")
    events = (_event(0), _event(1))
    assert isinstance(spool.store(events), StoreCommitted)
    duplicate = spool.store(events)
    loaded = spool.load_pending(limit=10)
    spool.close()

    assert duplicate == StoreCommitted(sequence_range=SequenceRange(0, 1), inserted_count=0)
    assert isinstance(loaded, LoadCommitted)
    assert loaded.events == events


def test_same_sequence_with_different_event_is_identity_conflict(tmp_path: Path) -> None:
    spool = _opened(tmp_path / "worker.sqlite")
    assert isinstance(spool.store((_event(0),)), StoreCommitted)
    conflicting = replace(_event(0), event_id=UUID(int=999))
    result = spool.store((conflicting,))
    spool.close()

    assert isinstance(result, StoreFailed)
    assert result.code is SpoolFailureCode.IDENTITY_CONFLICT


@pytest.mark.parametrize(
    ("events", "code"),
    (
        ((), SpoolFailureCode.EMPTY_BATCH),
        ((_event(1),), SpoolFailureCode.SEQUENCE_GAP),
        ((_event(0), _event(2)), SpoolFailureCode.NON_CONTIGUOUS_BATCH),
        ((_event(0, worker_id=OTHER_WORKER_ID),), SpoolFailureCode.WORKER_MISMATCH),
    ),
)
def test_store_rejects_invalid_batches(
    tmp_path: Path,
    events: tuple[EventEnvelope, ...],
    code: SpoolFailureCode,
) -> None:
    spool = _opened(tmp_path / "worker.sqlite")
    result = spool.store(events)
    spool.close()
    assert isinstance(result, StoreFailed)
    assert result.code is code


def test_ack_is_idempotent_and_compact_only_deletes_acknowledged_prefix(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    assert isinstance(spool.store(tuple(_event(index) for index in range(5))), StoreCommitted)
    assert spool.acknowledge(SequenceRange(0, 1)) == AckCommitted(SequenceRange(0, 1), updated_count=2)
    assert spool.acknowledge(SequenceRange(0, 1)) == AckCommitted(SequenceRange(0, 1), updated_count=0)
    assert spool.acknowledge(SequenceRange(3, 4)) == AckCommitted(SequenceRange(3, 4), updated_count=2)

    compacted = spool.compact_acknowledged_prefix()
    pending = spool.load_pending(limit=10)
    spool.close()

    assert compacted == CompactCommitted(deleted_count=2, through_sequence=1)
    assert isinstance(pending, LoadCommitted)
    assert pending.events == (_event(2),)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT worker_sequence, acknowledged FROM spool_events ORDER BY worker_sequence"
        ).fetchall() == [(2, 0), (3, 1), (4, 1)]


def test_load_pending_returns_first_contiguous_unacknowledged_range(tmp_path: Path) -> None:
    spool = _opened(tmp_path / "worker.sqlite")
    assert isinstance(spool.store(tuple(_event(index) for index in range(6))), StoreCommitted)
    assert isinstance(spool.acknowledge(SequenceRange(2, 3)), AckCommitted)
    loaded = spool.load_pending(limit=10)
    spool.close()
    assert loaded == LoadCommitted(sequence_range=SequenceRange(0, 1), events=(_event(0), _event(1)))


def test_open_io_failure_is_a_value(tmp_path: Path) -> None:
    def failing_factory(_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk unavailable")

    result = open_worker_spool(tmp_path / "worker.sqlite", WORKER_ID, connection_factory=failing_factory)
    assert isinstance(result, OpenFailed)
    assert result.code is SpoolFailureCode.SQLITE_ERROR
    assert "disk unavailable" in result.detail


def test_closed_store_returns_closed_before_batch_validation(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    spool.close()
    result = spool.store(())
    assert isinstance(result, StoreFailed)
    assert result.code is SpoolFailureCode.CLOSED
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM spool_events").fetchone() == (0,)


def test_all_operations_return_closed_after_close(tmp_path: Path) -> None:
    spool = _opened(tmp_path / "worker.sqlite")
    spool.close()
    store_result = spool.store((_event(0),))
    load_result = spool.load_pending(limit=1)
    ack_result = spool.acknowledge(SequenceRange(0, 0))
    compact_result = spool.compact_acknowledged_prefix()
    assert isinstance(store_result, StoreFailed) and store_result.code is SpoolFailureCode.CLOSED
    assert not isinstance(load_result, LoadCommitted) and load_result.code is SpoolFailureCode.CLOSED
    assert not isinstance(ack_result, AckCommitted) and ack_result.code is SpoolFailureCode.CLOSED
    assert not isinstance(compact_result, CompactCommitted) and compact_result.code is SpoolFailureCode.CLOSED


def test_store_sqlite_failure_rolls_back_and_is_a_value(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON spool_events BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    result = spool.store((_event(0),))
    spool.close()
    assert isinstance(result, StoreFailed)
    assert result.code is SpoolFailureCode.SQLITE_ERROR
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM spool_events").fetchone() == (0,)


def test_acknowledge_sqlite_failure_is_a_value(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    assert isinstance(spool.store((_event(0),)), StoreCommitted)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON spool_events BEGIN SELECT RAISE(ABORT, 'read only'); END"
        )
    result = spool.acknowledge(SequenceRange(0, 0))
    spool.close()
    assert not isinstance(result, AckCommitted)
    assert result.code is SpoolFailureCode.SQLITE_ERROR
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT acknowledged FROM spool_events").fetchone() == (0,)


def test_compact_sqlite_failure_is_a_value(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    assert isinstance(spool.store((_event(0),)), StoreCommitted)
    assert isinstance(spool.acknowledge(SequenceRange(0, 0)), AckCommitted)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_delete BEFORE DELETE ON spool_events BEGIN SELECT RAISE(ABORT, 'read only'); END"
        )
    result = spool.compact_acknowledged_prefix()
    spool.close()
    assert not isinstance(result, CompactCommitted)
    assert result.code is SpoolFailureCode.SQLITE_ERROR
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT worker_sequence FROM spool_events").fetchall() == [(0,)]


def test_load_sqlite_failure_is_a_value(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE spool_events")
    result = spool.load_pending(limit=10)
    spool.close()
    assert not isinstance(result, LoadCommitted)
    assert result.code is SpoolFailureCode.SQLITE_ERROR


def test_load_corrupt_frame_is_a_value(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    spool = _opened(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO spool_events(worker_sequence, event_id, frame) VALUES (0, 'corrupt', ?)",
            (b"\xff\xff\xff\xff",),
        )
    result = spool.load_pending(limit=10)
    spool.close()
    assert not isinstance(result, LoadCommitted)
    assert result.code is SpoolFailureCode.CORRUPT_FRAME


def test_new_worker_uses_independent_sequence_namespace(tmp_path: Path) -> None:
    first_path = tmp_path / "first.sqlite"
    second_path = tmp_path / "second.sqlite"
    first = _opened(first_path, WORKER_ID)
    second = _opened(second_path, OTHER_WORKER_ID)
    assert isinstance(first.store((_event(0),)), StoreCommitted)
    assert isinstance(second.store((_event(0, worker_id=OTHER_WORKER_ID),)), StoreCommitted)
    first.close()
    second.close()
    assert os.path.getsize(first_path) > 0
    assert os.path.getsize(second_path) > 0
