"""Regression tests for SpendCounterReseed shutdown short-circuits.

During shutdown the prisma engine is being torn down (or already dead from a
terminal SIGINT); reseed/window reads must skip the DB touch and return None
with a distinct log line instead of raising ClientNotConnectedError tracebacks
that spam the shutdown output. Without the guards, these calls proceed to
``await ...find_unique/group_by(...)`` on the (mocked/dead) client and blow up,
so ``result is None`` here is a real mutation-killing assertion.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from litellm.proxy.db.spend_counter_reseed import SpendCounterReseed
from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager


@pytest.fixture(autouse=True)
def _reset():
    GracefulShutdownManager.reset()
    yield
    GracefulShutdownManager.reset()


@pytest.mark.asyncio
async def test_from_db_skips_query_and_logs_distinctly_during_shutdown():
    GracefulShutdownManager.start_shutdown()
    prisma_client = MagicMock()
    with (
        patch("litellm.proxy.db.spend_counter_reseed.verbose_proxy_logger") as mock_log,
        patch("litellm.proxy.db.spend_counter_reseed.VerificationTokenRepository") as mock_repo,
    ):
        result = await SpendCounterReseed.from_db(prisma_client, "spend:key:abc")
    assert result is None
    assert "spend_counter_reseed_skipped_during_shutdown" in mock_log.info.call_args[0][0]
    # The DB repository is never even constructed -> no find_unique on the dead
    # client. Without this, dropping the early `return None` (but keeping the log)
    # survives, because from_db's own except swallows the resulting error to None.
    mock_repo.assert_not_called()


@pytest.mark.asyncio
async def test_window_from_spend_logs_skips_group_by_during_shutdown():
    GracefulShutdownManager.start_shutdown()
    prisma_client = MagicMock()
    with (
        patch("litellm.proxy.db.spend_counter_reseed.verbose_proxy_logger") as mock_log,
        patch("litellm.proxy.db.spend_counter_reseed.SpendLogsRepository") as mock_repo,
    ):
        result = await SpendCounterReseed.window_from_spend_logs(
            prisma_client, entity_type="Key", entity_id="abc", window_start=datetime(2026, 1, 1)
        )
    assert result is None
    assert "spend_counter_reseed_window_skipped_during_shutdown" in mock_log.info.call_args[0][0]
    mock_repo.assert_not_called()  # no group_by on the dead client
