"""Mock slow Anthropic /v1/messages upstream for downstream keepalive E2E.

Sends HTTP 200 + ``content-type: text/event-stream`` headers immediately, then
sleeps ``MOCK_TTFB_SECONDS`` before the first SSE byte (exercises keepalive
face 1 — TTFB), optionally sleeps ``MOCK_GAP_SECONDS`` mid-stream before the
text delta (face 2 — inter-chunk gap), then emits a spec-valid Anthropic
message stream. Headers flush before the first body chunk because Starlette
sends ``http.response.start`` before iterating the body generator, so litellm's
``httpx.post(stream=True)`` returns and then blocks reading the delayed body —
the exact condition create_response's keepalive path is built for.

Run: uvicorn mock_upstream:app --host 127.0.0.1 --port 8790
Env: MOCK_TTFB_SECONDS (default 8), MOCK_GAP_SECONDS (default 0)
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

TTFB = float(os.getenv("MOCK_TTFB_SECONDS", "8"))
GAP = float(os.getenv("MOCK_GAP_SECONDS", "0"))


def _frame(event: str, data: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def _message_stream(model: str):
    # Delay the FIRST byte (TTFB / face 1). Headers already went out.
    await asyncio.sleep(TTFB)
    yield _frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        },
    )
    yield _frame(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    # Optional mid-stream gap (face 2).
    if GAP > 0:
        await asyncio.sleep(GAP)
    yield _frame(
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello from mock"}},
    )
    yield _frame("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _frame(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}},
    )
    yield _frame("message_stop", {"type": "message_stop"})


@app.post("/v1/messages")
@app.post("/{full_path:path}/v1/messages")
async def messages(request: Request):
    body = await request.json()
    model = body.get("model", "claude-mock")
    if not body.get("stream"):
        # Non-streaming: return after TTFB so slow-path behavior is comparable.
        await asyncio.sleep(TTFB)
        return {
            "id": "msg_mock_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "hello from mock"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 4},
        }
    return StreamingResponse(_message_stream(model), media_type="text/event-stream")


@app.post("/v1/messages/count_tokens")
@app.post("/{full_path:path}/v1/messages/count_tokens")
async def count_tokens(request: Request):
    return {"input_tokens": 5}


@app.get("/health")
async def health():
    return {"status": "ok"}
