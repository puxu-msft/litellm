# Downstream SSE keepalive timeout PoC

## Decision

For the target streaming `/v1/messages` response, the body timeout is an inactivity/read timeout, not a fixed wall-clock deadline over the complete response body. Periodic SSE bytes therefore keep the downstream connection alive, provided every byte gap stays below the active idle threshold and intermediaries do not buffer the keepalive.

There is one premise correction: Claude Code 2.1.207 does not use the repository's Python Anthropic SDK/httpx process for its own request. Its bundled official TypeScript SDK uses Fetch. `API_TIMEOUT_MS` is passed as the SDK request timeout, but the SDK arms that timer only around `await fetch(...)` and clears it once Fetch returns the `Response`, so it covers connection/TLS/response headers rather than the entire streamed body. The streamed body is guarded separately by a byte-inactivity watchdog/runtime body idle timeout.

## Versions inspected

- Repository lock: `anthropic==0.84.0` at `uv.lock:244-257`
- Repository lock and installed environment: `httpx==0.28.1` at `uv.lock:2509-2518`
- Installed transport dependency: `httpcore==1.0.9`
- Locally installed Claude Code: `2.1.207`, binary `/home/xp/.local/share/claude/versions/2.1.207`

The repository `.venv` initially lacked the locked Anthropic package. The exact locked `anthropic==0.84.0` was installed into that project-local virtual environment for source inspection; no dependency declaration or LiteLLM source was changed.

## Python SDK/httpx evidence

`httpx.Timeout` exposes only `connect`, `read`, `write`, and `pool`; there is no `total` axis (`.venv/lib/python3.13/site-packages/httpx/_config.py:72-138`). A scalar default is copied into all four axes (`_config.py:127-130`). The client serializes exactly those axes into the request extension (`httpx/_client.py:371-377`, `584-591`).

For an HTTP/1.1 streaming body, HTTP Core reads `request.extensions["timeout"]["read"]` and invokes `_receive_event(timeout=timeout)` for every loop iteration (`httpcore/_async/http11.py:196-205`). When more network data is needed, each individual call invokes `network_stream.read(..., timeout=timeout)` (`http11.py:209-219`). The AnyIO backend wraps each `receive()` call in a new `anyio.fail_after(timeout)` scope (`httpcore/_backends/anyio.py:25-37`). HTTP/2 likewise reads the `read` value and applies it to each network read (`httpcore/_async/http2.py:433-442`). This is inactivity-between-reads behavior, not an absolute response deadline.

Anthropic Python SDK 0.84.0 sets `DEFAULT_TIMEOUT = httpx.Timeout(timeout=600, connect=5.0)` (`anthropic/_constants.py:8-10`), which expands to `connect=5`, `read=600`, `write=600`, and `pool=600`. Request construction passes that timeout to `httpx.build_request()` (`anthropic/_base_client.py:580-592`). The async stream path then calls `self._client.send(request, stream=True)` without replacing the timeout (`_base_client.py:1698-1716`), so the request extension's `read` axis controls body inactivity. A caller-provided scalar similarly expands through `httpx.Timeout(scalar)` to all four operation axes, not to a total axis.

## Claude Code evidence

Official environment-variable documentation says `API_TIMEOUT_MS` is the API request timeout, defaulting to 600000 ms, and separately documents `API_FORCE_IDLE_TIMEOUT` as the override for a five-minute timeout that aborts a streamed model response when no bytes arrive. It explicitly describes gateway connections as having that idle timeout active by default: <https://code.claude.com/docs/en/env-vars>.

The installed 2.1.207 binary corroborates this behavior:

- Client construction passes `timeout: parseInt(process.env.API_TIMEOUT_MS || String(600000), 10)` to the bundled official Anthropic TypeScript SDK.
- The bundled SDK's `fetchWithTimeout` uses `setTimeout(abort, ms)`, awaits Fetch, and clears the timer in `finally`. Fetch resolves after response headers, so this timer does not remain armed while consuming a streaming response body.
- Claude Code's stream wrapper resets its watchdog after every `reader.read()` result and reports `StreamIdleTimeoutError: stream idle: no bytes for ...ms` if no bytes arrive.
- Its transport configuration conditionally disables the runtime body timeout with `timeout: false` only when another byte watchdog is active or the idle timeout is explicitly disabled. For gateway/custom-provider paths, the documented default is the five-minute no-byte idle timeout.

The current upstream TypeScript SDK source shows the same timer lifecycle at `src/client.ts:1209-1253`: <https://github.com/anthropics/anthropic-sdk-typescript/blob/main/src/client.ts>.

Consequently, `API_TIMEOUT_MS` is not mapped to an httpx axis in Claude Code. It is a TypeScript SDK Fetch timer for the pre-body Fetch operation. The independently relevant streaming limit is byte inactivity.

## Local experiment

Run:

```shell
/home/xp/refs/ai-agents/litellm/.venv/bin/python /home/xp/refs/ai-agents/litellm/exp/downstream-keepalive-timeout/probe.py
```

Observed output:

```text
httpx=0.28.1
read_timeout=0.30s
keepalive outcome=completed elapsed=0.642s bytes=24 intervals=(0.2, 0.2, 0.2)
silent outcome=read-timeout elapsed=0.311s first_body_delay=0.45s
PASS: each received body chunk reset the inactivity read timeout
```

The keepalive scenario lasted more than twice the configured 0.30 s read timeout but completed because each `: ping\n\n` arrived after 0.20 s. The silent scenario timed out after approximately 0.31 s before its first body byte at 0.45 s.

## Operational constraints

Use a heartbeat interval comfortably below the shortest timeout in the full path; for Claude Code's documented five-minute body-idle limit, a much shorter interval such as 15-60 seconds leaves scheduling and network jitter margin. Emit and flush complete SSE comments such as `: ping\n\n`. An `event: ping` frame is also bytes and resets byte-level inactivity, but an SSE comment is less likely to surface as an application event.

Keepalives cannot defeat a separate absolute deadline imposed by LiteLLM, an ingress, load balancer, reverse proxy, NAT, or the upstream provider. No such total body deadline was found in the inspected httpx timeout model or Claude Code streaming path, but the complete deployment chain was not exercised here. Proxy buffering can also defeat this feature by withholding small heartbeat writes; buffering must be disabled and each heartbeat flushed.
