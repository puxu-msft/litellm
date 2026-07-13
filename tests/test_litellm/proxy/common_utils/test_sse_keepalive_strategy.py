import pytest

from litellm.proxy.common_utils.sse_keepalive import (
    ANTHROPIC_PING_EVENT,
    KEEPALIVE_COMMENT,
    AnthropicKeepaliveStrategy,
    CommentOnlyKeepaliveStrategy,
    frame_is_anthropic_message_start,
)


def test_comment_only_both_phases():
    s = CommentOnlyKeepaliveStrategy()
    assert s.idle_frames(seen_message_start=False) == (KEEPALIVE_COMMENT,)
    assert s.idle_frames(seen_message_start=True) == (KEEPALIVE_COMMENT,)
    assert s.observe_advances_to_phase2(b"event: message_start\ndata: {}\n\n") is False


def test_anthropic_phase1_comment_only_phase2_adds_ping():
    s = AnthropicKeepaliveStrategy()
    assert s.idle_frames(seen_message_start=False) == (KEEPALIVE_COMMENT,)
    assert s.idle_frames(seen_message_start=True) == (
        KEEPALIVE_COMMENT,
        ANTHROPIC_PING_EVENT,
    )


@pytest.mark.parametrize(
    "frame,expected",
    [
        (b'event: message_start\ndata: {"type":"message_start"}\n\n', True),
        ("event: message_start\ndata: {}\n\n", True),
        (b"event: content_block_delta\ndata: {}\n\n", False),
        (b'event: ping\ndata: {"type":"ping"}\n\n', False),
        # "message_start" appearing only inside data JSON must not count
        (b'event: content_block_delta\ndata: {"x":"message_start"}\n\n', False),
    ],
)
def test_frame_is_message_start(frame, expected):
    assert frame_is_anthropic_message_start(frame) is expected


def test_anthropic_observe_advances_only_on_message_start():
    s = AnthropicKeepaliveStrategy()
    assert s.observe_advances_to_phase2(b"event: message_start\ndata: {}\n\n") is True
    assert s.observe_advances_to_phase2(b"event: ping\ndata: {}\n\n") is False
