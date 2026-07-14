from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import litellm


@pytest.mark.asyncio
async def test_aresponses_establishes_http_client_deadline_on_logging_obj():
    captured = []
    original = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

    def _capture(self, deadline):
        captured.append(deadline)
        return original(self, deadline)

    with (
        patch(
            "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
            new=_capture,
            autospec=False,
        ),
        patch("litellm.responses.main.responses", new=AsyncMock(return_value=MagicMock())),
    ):
        await litellm.aresponses(model="github_copilot/gpt-4", input="hi", http_client={"total_timeout": 45.0})

    assert len(captured) == 1
    assert captured[0] is not None
