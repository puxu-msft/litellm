from __future__ import annotations

import struct
from dataclasses import replace

import orjson
import pytest
from pydantic import JsonValue, TypeAdapter

from litellm.proxy.observability.terminal.codec import (
    DEFAULT_MAX_FRAME_BYTES,
    DecoderFailed,
    EventFrameDecoder,
    FrameTooLarge,
    InvalidEvent,
    TruncatedFrame,
    UnsupportedSchema,
    encode_event_frame,
)
from litellm.proxy.observability.terminal.events import EventType
from litellm.proxy.observability.terminal.events import ALL_EVENT_TYPES, LifecycleEventPayload
from litellm.proxy.observability.terminal.events import FrozenJsonObject
from tests.test_litellm.proxy.observability.terminal.test_events import envelope

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_event_round_trip_preserves_unknown_top_level_fields() -> None:
    original = envelope(EventType.REQUEST_ROUTED)
    decoder = EventFrameDecoder()
    decoded = decoder.feed(encode_event_frame(original))
    decoder.finish()

    assert decoded == (original,)
    assert decoded[0].extensions == (("future_field", "kept"),)
    assert decoded[0].payload == original.payload


@pytest.mark.parametrize("event_type", tuple(ALL_EVENT_TYPES))
def test_every_event_payload_round_trips(event_type: EventType) -> None:
    original = envelope(event_type)
    decoder = EventFrameDecoder()
    assert decoder.feed(encode_event_frame(original)) == (original,)
    decoder.finish()


def test_decoder_accepts_every_single_cut_point() -> None:
    original = envelope(EventType.REQUEST_STREAMING)
    frame = encode_event_frame(original)
    for cut in range(1, len(frame)):
        decoder = EventFrameDecoder()
        assert decoder.feed(frame[:cut]) == ()
        assert decoder.feed(frame[cut:]) == (original,)
        decoder.finish()


def test_decoder_accepts_multiple_frames_in_one_chunk() -> None:
    first = envelope(EventType.REQUEST_ACCEPTED)
    second = envelope(EventType.REQUEST_COMPLETED)
    decoder = EventFrameDecoder()

    assert decoder.feed(encode_event_frame(first) + encode_event_frame(second)) == (first, second)
    decoder.finish()


def test_decoder_accepts_multiple_frames_one_byte_at_a_time() -> None:
    first = envelope(EventType.REQUEST_ACCEPTED)
    second = envelope(EventType.REQUEST_COMPLETED)
    decoder = EventFrameDecoder()
    decoded = ()
    for byte in encode_event_frame(first) + encode_event_frame(second):
        decoded = (*decoded, *decoder.feed(bytes((byte,))))
    decoder.finish()
    assert decoded == (first, second)


def test_decoder_rejects_oversized_frame_before_payload_arrives() -> None:
    decoder = EventFrameDecoder(max_frame_bytes=32)
    with pytest.raises(FrameTooLarge, match="33") as caught:
        decoder.feed(struct.pack("!I", 33))
    assert caught.value.size == 33
    assert caught.value.limit == 32
    with pytest.raises(DecoderFailed):
        decoder.feed(b"anything")


def test_decoder_treats_frame_size_as_unsigned_network_integer() -> None:
    decoder = EventFrameDecoder()
    with pytest.raises(FrameTooLarge) as caught:
        decoder.feed(b"\xff\xff\xff\xff")
    assert caught.value.size == 2**32 - 1


def test_encoder_honors_custom_frame_limit() -> None:
    original = envelope(EventType.REQUEST_ACCEPTED)
    encoded = encode_event_frame(original)
    payload_size = len(encoded) - 4
    assert encode_event_frame(original, max_frame_bytes=payload_size) == encoded
    with pytest.raises(FrameTooLarge):
        encode_event_frame(original, max_frame_bytes=payload_size - 1)


@pytest.mark.parametrize("tail", (b"\x00", b"\x00\x00\x00", struct.pack("!I", 10) + b"short"))
def test_decoder_rejects_truncated_tail_at_eof(tail: bytes) -> None:
    decoder = EventFrameDecoder()
    assert decoder.feed(tail) == ()
    with pytest.raises(TruncatedFrame):
        decoder.finish()


def test_decoder_rejects_unknown_schema_without_losing_raw_fields() -> None:
    raw = {
        "schema_version": 99,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "event_type": "request.accepted",
        "worker_instance_id": "00000000-0000-4000-8000-000000000002",
        "worker_sequence": 1,
        "occurred_at_utc": "2026-07-18T12:34:56Z",
        "payload": {"stage": "accepted"},
        "future_field": {"nested": True},
    }
    payload = orjson.dumps(raw)
    decoder = EventFrameDecoder()

    with pytest.raises(UnsupportedSchema) as caught:
        decoder.feed(struct.pack("!I", len(payload)) + payload)

    assert caught.value.schema_version == 99
    future_field = caught.value.raw_event["future_field"]
    assert isinstance(future_field, FrozenJsonObject)
    assert future_field["nested"] is True


def test_encoder_rejects_top_level_extension_collision() -> None:
    original = envelope(EventType.REQUEST_ACCEPTED)
    collided = replace(original, extensions=(("event_type", "wrong"),))
    with pytest.raises(ValueError, match="event_type"):
        encode_event_frame(collided)


def test_encoder_rejects_payload_extension_collision() -> None:
    original = envelope(EventType.REQUEST_ACCEPTED)
    collided = replace(
        original,
        payload=LifecycleEventPayload(stage="accepted", extensions=(("stage", "wrong"),)),
    )
    with pytest.raises(ValueError, match="stage"):
        encode_event_frame(collided)


def test_default_frame_limit_is_bounded() -> None:
    assert DEFAULT_MAX_FRAME_BYTES == 8 * 1024 * 1024


def test_decoder_rejects_boolean_integer_fields() -> None:
    raw = _JSON_OBJECT_ADAPTER.validate_json(encode_event_frame(envelope(EventType.REQUEST_ACCEPTED))[4:])
    payload = orjson.dumps({**raw, "worker_sequence": True})
    decoder = EventFrameDecoder()
    with pytest.raises(InvalidEvent, match="worker_sequence must be an integer"):
        decoder.feed(struct.pack("!I", len(payload)) + payload)
