#!/usr/bin/env bash
# A-tier deterministic E2E: mock slow upstream + real litellm proxy + tight-read
# probe. Proves the SAME create_response keepalive code path (route_type
# anthropic_messages) injects keepalive during a slow TTFB, so a client with a
# read timeout SHORTER than the upstream TTFB survives with keepalive ON and
# times out with keepalive OFF.
#
# Usage: ./run-a.sh
# Requires: repo .venv with litellm[proxy], fastapi, uvicorn, httpx.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
PY="$ROOT/.venv/bin/python"
UVICORN="$ROOT/.venv/bin/uvicorn"
LITELLM="$ROOT/.venv/bin/litellm"

MOCK_PORT=8790
PROXY_PORT=4000
export MOCK_TTFB_SECONDS="${MOCK_TTFB_SECONDS:-8}"
export MOCK_GAP_SECONDS="${MOCK_GAP_SECONDS:-0}"
READ_TIMEOUT="${READ_TIMEOUT:-5}"

pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT

wait_health() { # url
  for _ in $(seq 1 60); do
    curl -sf "$1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "timed out waiting for $1"; return 1
}

start_mock() {
  "$UVICORN" mock_upstream:app --host 127.0.0.1 --port "$MOCK_PORT" --log-level warning &
  pids+=($!)
  wait_health "http://127.0.0.1:$MOCK_PORT/health" || exit 1
  echo "mock up (TTFB=${MOCK_TTFB_SECONDS}s GAP=${MOCK_GAP_SECONDS}s)"
}

start_proxy() { # config
  "$LITELLM" --config "$1" --port "$PROXY_PORT" --num_workers 1 >/tmp/litellm-keepalive-$$.log 2>&1 &
  PROXY_PID=$!
  pids+=("$PROXY_PID")
  wait_health "http://127.0.0.1:$PROXY_PORT/health/liveliness" || { cat /tmp/litellm-keepalive-$$.log; exit 1; }
  echo "proxy up ($1)"
}

stop_proxy() { kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null; }

echo "=== A-tier: mock TTFB ${MOCK_TTFB_SECONDS}s vs client read timeout ${READ_TIMEOUT}s ==="
start_mock

echo; echo "--- keepalive ON: expect survive (pings reset the read timer) ---"
start_proxy config-on.yaml
"$PY" probe.py --expect survive --read-timeout "$READ_TIMEOUT"; on_rc=$?
stop_proxy

echo; echo "--- keepalive OFF: expect timeout (proxy holds response during TTFB) ---"
start_proxy config-off.yaml
"$PY" probe.py --expect timeout --read-timeout "$READ_TIMEOUT"; off_rc=$?
stop_proxy

echo
if [ "$on_rc" -eq 0 ] && [ "$off_rc" -eq 0 ]; then
  echo "=== A-tier PASS: keepalive ON survived, keepalive OFF timed out ==="
  exit 0
fi
echo "=== A-tier FAIL (on_rc=$on_rc off_rc=$off_rc) ==="
exit 1
