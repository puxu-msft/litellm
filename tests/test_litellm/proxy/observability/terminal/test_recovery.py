from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from litellm.proxy.observability.terminal.archive.catalog import CatalogOpened, SegmentState, open_catalog
from litellm.proxy.observability.terminal.archive.recovery import (
    RecoveredRequest,
    RecoveryCompleted,
    reconcile_archive,
)
from litellm.proxy.observability.terminal.archive.segment import SegmentManagerOpened, open_segment_manager
from litellm.proxy.observability.terminal.events import TerminalReason


WORKER_ID = UUID("00000000-0000-4000-8000-000000000401")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000402")


def test_recovery_finishes_dead_worker_request_as_shutdown_dropped(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path,
        catalog_result.catalog,
        clock=lambda: 0,
        max_age_ns=100,
        max_bytes=1_000_000,
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    segment_id = manager.active_segment_id
    manager.bind_request(REQUEST_ID, WORKER_ID)
    manager.close()

    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result == RecoveryCompleted(
        recovered_requests=(RecoveredRequest(REQUEST_ID, segment_id, TerminalReason.SHUTDOWN_DROPPED),),
        published_segments=(),
        orphan_blobs_deleted=0,
    )
    assert catalog_result.catalog.open_requests(segment_id) == ()
    catalog_result.catalog.close()


def test_recovery_does_not_infer_terminal_for_live_worker(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path,
        catalog_result.catalog,
        clock=lambda: 0,
        max_age_ns=100,
        max_bytes=1_000_000,
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    segment_id = manager.active_segment_id
    manager.bind_request(REQUEST_ID, WORKER_ID)
    manager.close()
    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset({WORKER_ID}))
    assert result == RecoveryCompleted((), (), 0)
    assert catalog_result.catalog.open_requests(segment_id) == ((REQUEST_ID, WORKER_ID),)
    catalog_result.catalog.close()


def test_recovery_reconciles_renamed_publishing_segment(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path,
        catalog_result.catalog,
        clock=lambda: 0,
        max_age_ns=100,
        max_bytes=1_000_000,
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    old = manager.active_segment_id
    manager.rotate_force()
    active_path = manager.segment_path(old)
    final_path = active_path.with_name(f"{old}.segment.sqlite")
    catalog_result.catalog.transition_segment(old, SegmentState.PUBLISHING, path=str(final_path.relative_to(tmp_path)))
    active_path.rename(final_path)
    manager.close()

    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result.published_segments == (old,)
    assert catalog_result.catalog.get_segment(old).state is SegmentState.PUBLISHED
    catalog_result.catalog.close()


def test_recovery_completes_rename_when_catalog_was_updated_first(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path, catalog_result.catalog, clock=lambda: 0, max_age_ns=100, max_bytes=1_000_000
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    old = manager.active_segment_id
    manager.rotate_force()
    active_path = manager.segment_path(old)
    final_path = active_path.with_name(f"{old}.segment.sqlite")
    catalog_result.catalog.transition_segment(old, SegmentState.PUBLISHING, path=str(final_path.relative_to(tmp_path)))
    manager.close()

    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result.published_segments == (old,)
    assert final_path.exists()
    assert not active_path.exists()
    assert catalog_result.catalog.get_segment(old).state is SegmentState.PUBLISHED
    catalog_result.catalog.close()


def test_recovery_does_not_delete_unreferenced_blob_from_active_segment(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path,
        catalog_result.catalog,
        clock=lambda: 0,
        max_age_ns=100,
        max_bytes=1_000_000,
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    segment = manager.active_segment_id
    with sqlite3.connect(manager.segment_path(segment)) as connection:
        connection.execute("INSERT INTO content_blobs(digest, byte_count, compressed) VALUES ('b3:orphan', 0, x'00')")
    manager.close()
    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result.orphan_blobs_deleted == 0
    with sqlite3.connect(tmp_path / catalog_result.catalog.get_segment(segment).path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone() == (1,)
    catalog_result.catalog.close()


def test_recovery_deletes_orphan_from_closed_draining_segment(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path, catalog_result.catalog, clock=lambda: 0, max_age_ns=100, max_bytes=1_000_000
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    segment = manager.active_segment_id
    manager.rotate_force()
    with sqlite3.connect(manager.segment_path(segment)) as connection:
        connection.execute("INSERT INTO content_blobs(digest,byte_count,compressed) VALUES ('b3:orphan',0,x'00')")
    manager.close()
    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result.orphan_blobs_deleted == 1
    catalog_result.catalog.close()


def test_recovery_reports_publishing_segment_with_no_files(tmp_path: Path) -> None:
    catalog_result = open_catalog(tmp_path / "catalog.sqlite")
    assert isinstance(catalog_result, CatalogOpened)
    manager_result = open_segment_manager(
        tmp_path, catalog_result.catalog, clock=lambda: 0, max_age_ns=100, max_bytes=1_000_000
    )
    assert isinstance(manager_result, SegmentManagerOpened)
    manager = manager_result.manager
    old = manager.active_segment_id
    manager.rotate_force()
    active_path = manager.segment_path(old)
    final_path = active_path.with_name(f"{old}.segment.sqlite")
    catalog_result.catalog.transition_segment(old, SegmentState.PUBLISHING, path=str(final_path.relative_to(tmp_path)))
    active_path.unlink()
    manager.close()
    result = reconcile_archive(tmp_path, catalog_result.catalog, live_workers=frozenset())
    assert result.missing_segment_files == (old,)
    assert catalog_result.catalog.get_segment(old).state is SegmentState.PUBLISHING
    catalog_result.catalog.close()
