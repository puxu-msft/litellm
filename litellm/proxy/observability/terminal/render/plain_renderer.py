from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

import orjson
from pydantic import JsonValue, TypeAdapter

from litellm.proxy.observability.terminal.codec import encode_event_frame
from litellm.proxy.observability.terminal.events import EventEnvelope


class TextSink(Protocol):
    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class JsonLineSink:
    output: TextSink

    def emit(self, event: EventEnvelope) -> None:
        payload = encode_event_frame(event)[4:]
        self.output.write(payload.decode("utf-8") + "\n")
        self.output.flush()


@dataclass(frozen=True, slots=True)
class PlainLineSink:
    output: TextSink

    def emit(self, event: EventEnvelope) -> None:
        line = f"{event.occurred_at_utc.isoformat()} {event.event_type.value} worker={event.worker_instance_id}"
        self.output.write(line + "\n")
        self.output.flush()


ValidatedJson: TypeAlias = JsonValue
_JSON_VALUE: TypeAdapter[ValidatedJson] = TypeAdapter(ValidatedJson)


def parse_json_line(line: str) -> JsonValue:
    return _JSON_VALUE.validate_python(orjson.loads(line))
