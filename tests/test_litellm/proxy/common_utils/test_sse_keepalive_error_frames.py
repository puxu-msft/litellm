import json

import pytest

from litellm.proxy.common_utils.sse_keepalive import (
    DownstreamSSESurface,
    committed_error_frame,
)


def _error_obj():
    return {"message": "boom", "type": "internal", "param": "None", "code": "500"}


def test_anthropic_committed_error_frame_uses_event_error():
    frame = committed_error_frame(DownstreamSSESurface.ANTHROPIC, _error_obj())
    assert frame.startswith("event: error\n")
    assert frame.endswith("\n\n")
    data_line = [ln for ln in frame.split("\n") if ln.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: ") :])
    # Anthropic SDK only surfaces streamed errors when type == "error"
    assert payload["type"] == "error"
    assert payload["error"] == _error_obj()


@pytest.mark.parametrize(
    "surface",
    [DownstreamSSESurface.OPENAI_CHAT, DownstreamSSESurface.OPENAI_RESPONSES],
)
def test_openai_committed_error_frame_is_data_error(surface):
    frame = committed_error_frame(surface, _error_obj())
    assert not frame.startswith("event:")  # OpenAI SDK reads a bare data: {"error"} frame
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame[len("data: ") : -len("\n\n")])
    assert payload["error"] == _error_obj()
