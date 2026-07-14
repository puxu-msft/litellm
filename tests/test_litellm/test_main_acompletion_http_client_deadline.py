from unittest.mock import AsyncMock, patch

import pytest

import litellm


@pytest.mark.asyncio
async def test_acompletion_establishes_http_client_deadline_on_logging_obj():
    captured_logging_objs = []

    original_get_logging_id = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

    def _capture(self, deadline):
        captured_logging_objs.append((self, deadline))
        return original_get_logging_id(self, deadline)

    with (
        patch(
            "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
            new=_capture,
            autospec=False,
        ),
        patch("litellm.main.completion", new=AsyncMock(return_value={"choices": []})),
    ):
        await litellm.acompletion(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            http_client={"total_timeout": 30.0},
        )

    assert len(captured_logging_objs) == 1
    _, deadline = captured_logging_objs[0]
    assert deadline is not None


@pytest.mark.asyncio
async def test_acompletion_leaves_deadline_none_without_http_client_config():
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
        patch("litellm.main.completion", new=AsyncMock(return_value={"choices": []})),
    ):
        await litellm.acompletion(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

    assert captured == [None]
