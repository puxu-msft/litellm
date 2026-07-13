import logging

import pytest

from litellm.proxy.common_utils.sse_frame_normalizer import (
    find_frame_delimiter,
    normalize_anthropic_sse_frames,
)


async def _aiter(chunks):
    for c in chunks:
        yield c


@pytest.mark.parametrize(
    "buf,expected_end",
    [
        (b"event: ping\n\n", len(b"event: ping\n\n")),
        (b"a\r\n\r\nb", len(b"a\r\n\r\n")),
        (b"a\r\rb", len(b"a\r\r")),
        (b"no delimiter yet", -1),
        (b"", -1),
    ],
)
def test_find_frame_delimiter(buf, expected_end):
    assert find_frame_delimiter(buf) == expected_end


def test_find_frame_delimiter_does_not_cut_inside_crlf_delimiter():
    # \n\n starts later than \r\n\r\n; must not split inside the \r\n\r\n delimiter
    buf = b"a\r\n\r\nb"
    end = find_frame_delimiter(buf)
    assert buf[:end] == b"a\r\n\r\n"
    assert buf[end:] == b"b"


@pytest.mark.asyncio
async def test_reassembles_frame_split_mid_json():
    chunks = [b'data: {"text":"hel', b'lo"}\n\n']
    out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b'data: {"text":"hello"}\n\n']


@pytest.mark.asyncio
async def test_preserves_multibyte_utf8_split_across_chunks():
    emoji = "🎉".encode("utf-8")  # 4 bytes
    chunks = [b"data: " + emoji[:2], emoji[2:] + b"\n\n"]
    out = b"".join([f async for f in normalize_anthropic_sse_frames(_aiter(chunks))])
    assert out == b"data: " + emoji + b"\n\n"  # byte-identical
    assert b"\xef\xbf\xbd" not in out  # U+FFFD replacement char never introduced


@pytest.mark.asyncio
async def test_two_frames_in_one_chunk_not_glued():
    chunks = [b"event: a\n\nevent: b\n\n"]
    out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b"event: a\n\n", b"event: b\n\n"]


@pytest.mark.asyncio
async def test_eof_trailing_remainder_flushed_and_logged(caplog):
    chunks = [b"data: trailing-no-delim"]
    with caplog.at_level(logging.DEBUG, logger="litellm"):
        out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b"data: trailing-no-delim"]


@pytest.mark.asyncio
async def test_no_trailing_flush_when_clean_boundary():
    chunks = [b"event: a\n\n"]
    out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b"event: a\n\n"]  # nothing extra flushed


@pytest.mark.asyncio
async def test_unterminated_over_limit_raises():
    chunks = [b"x" * 10]
    with pytest.raises(ValueError):
        [
            f
            async for f in normalize_anthropic_sse_frames(
                _aiter(chunks), max_unterminated_bytes=4
            )
        ]
