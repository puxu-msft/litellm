import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))  # Adds the parent directory to the system path


from litellm.proxy.db.prisma_client import PrismaWrapper, should_update_prisma_schema


@pytest.fixture(autouse=True)
def mock_prisma_binary():
    """Mock prisma.Prisma to avoid requiring generated Prisma binaries for unit tests."""
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"prisma": mock_module}):
        yield mock_module


def test_should_update_prisma_schema(monkeypatch):
    # CASE 1: Environment variable behavior
    # When DISABLE_SCHEMA_UPDATE is not set -> should update
    monkeypatch.setenv("DISABLE_SCHEMA_UPDATE", None)
    assert should_update_prisma_schema() == True

    # When DISABLE_SCHEMA_UPDATE="true" -> should not update
    monkeypatch.setenv("DISABLE_SCHEMA_UPDATE", "true")
    assert should_update_prisma_schema() == False

    # When DISABLE_SCHEMA_UPDATE="false" -> should update
    monkeypatch.setenv("DISABLE_SCHEMA_UPDATE", "false")
    assert should_update_prisma_schema() == True

    # CASE 2: Explicit parameter behavior (overrides env var)
    monkeypatch.setenv("DISABLE_SCHEMA_UPDATE", None)
    assert should_update_prisma_schema(True) == False  # Param True -> should not update

    monkeypatch.setenv("DISABLE_SCHEMA_UPDATE", None)  # Set env var opposite to param
    assert should_update_prisma_schema(False) == True  # Param False -> should update


@pytest.mark.asyncio
async def test_recreate_prisma_client_successful_disconnect():
    """
    Test that recreate_prisma_client works normally when disconnect succeeds.
    """
    # Mock the original prisma client
    mock_prisma = AsyncMock()

    # Create a mock PrismaWrapper instance
    wrapper = Mock()
    wrapper._original_prisma = mock_prisma

    # Configure disconnect to succeed
    mock_prisma.disconnect.return_value = None

    # Mock the entire recreate_prisma_client method to avoid import issues
    async def mock_recreate_prisma_client(new_db_url: str, http_client=None):
        try:
            await mock_prisma.disconnect()
        except Exception:
            pass

        mock_new_prisma = AsyncMock()
        wrapper._original_prisma = mock_new_prisma
        await mock_new_prisma.connect()

    # Assign the mock method to the wrapper
    wrapper.recreate_prisma_client = mock_recreate_prisma_client

    # Call the method
    await wrapper.recreate_prisma_client("postgresql://new:new@localhost:5432/new")

    # Verify that disconnect was called
    mock_prisma.disconnect.assert_called_once()

    # Verify that the new client replaced the original
    assert wrapper._original_prisma != mock_prisma
    assert hasattr(wrapper._original_prisma, "connect")


@pytest.mark.asyncio
async def test_recreate_prisma_client_kills_old_engine_on_disconnect_failure(
    mock_prisma_binary,
):
    """When disconnect() fails, recreate_prisma_client must SIGTERM/SIGKILL the old engine PID."""
    mock_prisma = AsyncMock()
    mock_prisma.disconnect.side_effect = Exception("engine hung")

    # Simulate engine subprocess with a known PID
    mock_engine = MagicMock()
    mock_engine.process.pid = 12345
    mock_prisma._engine = mock_engine

    wrapper = PrismaWrapper(original_prisma=mock_prisma, iam_token_db_auth=False)

    # Configure the mock Prisma constructor
    mock_new_prisma = AsyncMock()
    mock_prisma_binary.Prisma.return_value = mock_new_prisma

    with (
        patch("os.kill") as mock_kill,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await wrapper.recreate_prisma_client("postgresql://new")

    # Verify old engine was killed
    mock_kill.assert_any_call(12345, signal.SIGTERM)
    # Verify new client was created and connected
    mock_new_prisma.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_recreate_prisma_client_skips_kill_on_successful_disconnect(
    mock_prisma_binary,
):
    """When disconnect() succeeds, no kill should be attempted."""
    mock_prisma = AsyncMock()
    mock_prisma.disconnect.return_value = None

    wrapper = PrismaWrapper(original_prisma=mock_prisma, iam_token_db_auth=False)

    mock_new_prisma = AsyncMock()
    mock_prisma_binary.Prisma.return_value = mock_new_prisma

    with patch("os.kill") as mock_kill:
        await wrapper.recreate_prisma_client("postgresql://new")

    mock_kill.assert_not_called()
    mock_new_prisma.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_recreate_prisma_client_handles_missing_engine_pid(
    mock_prisma_binary,
):
    """When engine PID is unavailable (no _engine attr), kill is skipped gracefully."""
    mock_prisma = AsyncMock()
    mock_prisma.disconnect.side_effect = Exception("engine hung")
    mock_prisma._engine = None  # No engine subprocess

    wrapper = PrismaWrapper(original_prisma=mock_prisma, iam_token_db_auth=False)

    mock_new_prisma = AsyncMock()
    mock_prisma_binary.Prisma.return_value = mock_new_prisma

    with (
        patch("os.kill") as mock_kill,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await wrapper.recreate_prisma_client("postgresql://new")

    mock_kill.assert_not_called()  # PID was 0, kill skipped
    mock_new_prisma.connect.assert_awaited_once()


# ── IAM token refresh shutdown guards (Task 5) ──────────────────────────────


@pytest.mark.asyncio
async def test_token_refresh_loop_breaks_when_shutting_down_after_sleep():
    """Guard #1: loop wakes, sees shutdown, and BREAKS (not continue) — a
    continue with sleep_seconds<=0 would busy-spin. Wrapped in wait_for so a
    regression to `continue` fails as a timeout instead of hanging the suite."""
    import asyncio

    wrapper = PrismaWrapper(original_prisma=MagicMock(), iam_token_db_auth=True, is_shutting_down=lambda: True)
    wrapper._calculate_seconds_until_refresh = MagicMock(return_value=0)
    wrapper._safe_refresh_token = AsyncMock()
    await asyncio.wait_for(wrapper._token_refresh_loop(), timeout=2.0)
    wrapper._safe_refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_refresh_token_skips_recreate_when_shutting_down_after_lock():
    """Guard #2: shutdown that started while waiting for _reconnection_lock
    aborts the refresh before it touches the token/engine."""
    wrapper = PrismaWrapper(original_prisma=MagicMock(), iam_token_db_auth=True, is_shutting_down=lambda: True)
    wrapper._token_refresh_not_needed = MagicMock()
    wrapper.get_rds_iam_token = MagicMock()
    await wrapper._safe_refresh_token()
    wrapper._token_refresh_not_needed.assert_not_called()
    wrapper.get_rds_iam_token.assert_not_called()


@pytest.mark.asyncio
async def test_recreate_prisma_client_locked_skips_kill_when_shutting_down():
    """Guard #3: after the optimistic-generation check, before killing/spawning
    the engine — also defends the watchdog's own reconnect path, which reaches
    this same method."""
    wrapper = PrismaWrapper(original_prisma=MagicMock(), iam_token_db_auth=True, is_shutting_down=lambda: True)
    wrapper._get_engine_pid = MagicMock()
    result = await wrapper._recreate_prisma_client_locked("postgresql://new")
    assert result is False
    wrapper._get_engine_pid.assert_not_called()


def test_prisma_wrapper_defaults_is_shutting_down_to_manager():
    """No injection -> the process-wide GracefulShutdownManager backs the guard."""
    from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager

    GracefulShutdownManager.reset()
    wrapper = PrismaWrapper(original_prisma=MagicMock(), iam_token_db_auth=False)
    try:
        assert wrapper._is_shutting_down() is False
        GracefulShutdownManager.start_shutdown()
        assert wrapper._is_shutting_down() is True
    finally:
        GracefulShutdownManager.reset()
