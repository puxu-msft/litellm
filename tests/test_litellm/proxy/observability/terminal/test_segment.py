from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.archive.catalog import CatalogOpened, SegmentState, open_catalog
from litellm.proxy.observability.terminal.archive.segment import (
    SegmentManager,
    SegmentManagerOpened,
    SegmentIncomplete,
    SegmentNotSealable,
    SegmentSealed,
    open_segment_manager,
)


@dataclass
class Clock:
    value: int

    def __call__(self) -> int:
        return self.value


WORKER_ID = UUID("00000000-0000-4000-8000-000000000301")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000302")


def _manager(root: Path, clock: Clock, *, max_age_ns: int = 100, max_bytes: int = 1_000_000) -> SegmentManager:
    catalog_result = open_catalog(root / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    result = open_segment_manager(root, catalog_result.catalog, clock=clock, max_age_ns=max_age_ns, max_bytes=max_bytes)
    assert isinstance(result, SegmentManagerOpened)
    return result.manager


def test_rotation_binds_existing_request_to_draining_and_new_request_to_active(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    first_segment = manager.active_segment_id
    manager.bind_request(REQUEST_ID, WORKER_ID)
    clock.value = 101
    rotated = manager.rotate_if_needed()
    assert rotated is not None
    assert rotated == first_segment
    second_segment = manager.active_segment_id
    assert second_segment != first_segment
    second_request = UUID("00000000-0000-4000-8000-000000000303")
    manager.bind_request(second_request, WORKER_ID)
    assert manager.owner_segment(REQUEST_ID) == first_segment
    assert manager.owner_segment(second_request) == second_segment
    assert manager.segment_state(first_segment) is SegmentState.DRAINING
    manager.close()


def test_draining_segment_cannot_seal_until_owner_terminal(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    old = manager.active_segment_id
    manager.bind_request(REQUEST_ID, WORKER_ID)
    clock.value = 101
    manager.rotate_if_needed()
    assert manager.seal(old) == SegmentNotSealable(open_request_count=1)
    manager.mark_request_terminal(REQUEST_ID, "completed")
    sealed = manager.seal(old)
    assert isinstance(sealed, SegmentSealed)
    assert sealed.path.suffix == ".sqlite"
    assert sealed.path.name.endswith(".segment.sqlite")
    assert manager.segment_state(old) is SegmentState.PUBLISHED
    manager.close()


def test_draining_segment_cannot_seal_with_missing_referenced_blob(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    old = manager.active_segment_id
    clock.value = 101
    manager.rotate_if_needed()
    with sqlite3.connect(manager.segment_path(old)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO body_chunk_refs(blob_digest) VALUES ('b3:missing')")
    result = manager.seal(old)
    assert result == SegmentIncomplete(missing_digests=("b3:missing",), corrupt_digests=())
    assert manager.segment_state(old) is SegmentState.DRAINING
    manager.close()


def test_draining_segment_cannot_seal_with_corrupt_referenced_blob(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    old = manager.active_segment_id
    clock.value = 101
    manager.rotate_if_needed()
    with sqlite3.connect(manager.segment_path(old)) as connection:
        connection.execute("INSERT INTO content_blobs(digest,byte_count,compressed) VALUES ('b3:corrupt',1,x'00')")
        connection.execute("INSERT INTO body_chunk_refs(blob_digest) VALUES ('b3:corrupt')")
    result = manager.seal(old)
    assert result == SegmentIncomplete(missing_digests=(), corrupt_digests=("b3:corrupt",))
    assert manager.segment_state(old) is SegmentState.DRAINING
    manager.close()


def test_size_threshold_rotates(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock, max_age_ns=10_000, max_bytes=1)
    old = manager.active_segment_id
    with sqlite3.connect(manager.segment_path(old)) as connection:
        connection.execute("CREATE TABLE payload(value BLOB)")
        connection.execute("INSERT INTO payload VALUES (?)", (b"large",))
    assert manager.rotate_if_needed() == old
    manager.close()


def test_stale_owner_does_not_force_seal(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    old = manager.active_segment_id
    manager.bind_request(REQUEST_ID, WORKER_ID)
    clock.value = 101
    manager.rotate_if_needed()
    clock.value = 10_000
    assert manager.seal(old) == SegmentNotSealable(open_request_count=1)
    manager.close()


def test_request_cannot_change_owner_segment(tmp_path: Path) -> None:
    clock = Clock(0)
    manager = _manager(tmp_path, clock)
    manager.bind_request(REQUEST_ID, WORKER_ID)
    clock.value = 101
    manager.rotate_if_needed()
    with pytest.raises(ValueError, match="already bound"):
        manager.bind_request(REQUEST_ID, WORKER_ID)
    manager.close()
