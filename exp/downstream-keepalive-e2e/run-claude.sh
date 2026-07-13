#!/usr/bin/env bash
# B-tier: real Claude Code against the litellm proxy + slow mock upstream.
#
# Uses REALISTIC timeouts (no shrinking): the mock delays first byte past
# Claude Code's default 300s idle timeout (API_FORCE_IDLE_TIMEOUT), and the
# proxy pings every 15s to keep the client alive. This starts the mock + proxy
# and prints the exact `claude` command to run in a second terminal.
#
# To see the CONTRAST, run once with config-b-realistic.yaml (keepalive ON ->
# Claude survives the long TTFB) and once with config-off.yaml (keepalive OFF
# -> Claude aborts at ~300s).
#
# Usage: ./run-claude.sh [on|off]   (default on)
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
UVICORN="$ROOT/.venv/bin/uvicorn"
LITELLM="$ROOT/.venv/bin/litellm"

MODE="${1:-on}"
if [ "$MODE" = "off" ]; then CONFIG=config-off.yaml; else CONFIG=config-b-realistic.yaml; fi

MOCK_PORT=8790
PROXY_PORT=4000
# TTFB just over the 300s default idle timeout so the difference is unambiguous.
export MOCK_TTFB_SECONDS="${MOCK_TTFB_SECONDS:-330}"
export MOCK_GAP_SECONDS="${MOCK_GAP_SECONDS:-0}"

pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT

wait_health() { for _ in $(seq 1 60); do curl -sf "$1" >/dev/null 2>&1 && return 0; sleep 0.5; done; echo "timeout $1"; return 1; }

"$UVICORN" mock_upstream:app --host 127.0.0.1 --port "$MOCK_PORT" --log-level warning &
pids+=($!); wait_health "http://127.0.0.1:$MOCK_PORT/health" || exit 1
echo "mock up (TTFB=${MOCK_TTFB_SECONDS}s)"

"$LITELLM" --config "$CONFIG" --port "$PROXY_PORT" --num_workers 1 &
pids+=($!); wait_health "http://127.0.0.1:$PROXY_PORT/health/liveliness" || exit 1
echo "proxy up ($CONFIG)"

cat <<EOF

=== Ready. In another terminal, launch real Claude Code against the proxy: ===

  ANTHROPIC_BASE_URL=http://127.0.0.1:$PROXY_PORT \\
  ANTHROPIC_API_KEY=sk-keepalive-test \\
  ANTHROPIC_MODEL=test-claude \\
  claude

Then send any message. Mode=$MODE, mock first-byte delay=${MOCK_TTFB_SECONDS}s.
  - keepalive ON  (default): pings every 15s -> Claude survives the ${MOCK_TTFB_SECONDS}s wait, replies "hello from mock".
  - keepalive OFF (./run-claude.sh off): Claude aborts around its 300s idle timeout.

Notes:
  - Claude Code may send /v1/messages/count_tokens first; the mock answers it.
  - If Claude rejects the model name, it is passed through ANTHROPIC_MODEL=test-claude above.
  - Ctrl-C here to tear down mock + proxy.
EOF

wait
