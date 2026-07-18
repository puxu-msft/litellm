#!/usr/bin/env bash

# Optional: Custom token directory
export GITHUB_COPILOT_TOKEN_DIR="$HOME/.config/litellm/github_copilot"
# Optional: Custom access token file name
export GITHUB_COPILOT_ACCESS_TOKEN_FILE="access-token"
# Optional: Custom API key file name
# export GITHUB_COPILOT_API_KEY_FILE="api-key.json"

# Optional: Custom Copilot endpoints for authentication and usage
# (needed when using GitHub Enterprise subscriptions with custom endpoints or self-hosted GitHub servers
export GITHUB_COPILOT_API_BASE="https://api.enterprise.githubcopilot.com"
# export GITHUB_COPILOT_DEVICE_CODE_URL="https://my-company.ghe.com/login/device/code"
# export GITHUB_COPILOT_ACCESS_TOKEN_URL="https://my-company.ghe.com/login/oauth/access_token"
# export GITHUB_COPILOT_API_KEY_URL="https://my-company.ghe.com/api/v3/copilot_internal/v2/token"

export REDIS_HOST="127.0.0.1"
export REDIS_PORT="6379"
export REDIS_PASSWORD=

export AIOHTTP_KEEPALIVE_TIMEOUT=900

export GHC_REASONING_POC=1

# Durable shadow event archive. The existing hookpkg request log remains the
# only TTY owner until the Rich renderer cutover gate is completed.
export LITELLM_TERMINAL_ARCHIVE_DIR="$HOME/.local/share/litellm/terminal-events"

pushd "$(dirname "$0")" || exit 1

uv run --no-sync -- litellm --config "$HOME/.config/litellm/config.yaml" --host 127.0.0.1 --port 4142

popd
