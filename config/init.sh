#!/usr/bin/env bash

brew install postgresql redis
brew services start postgresql
brew services start redis

createdb litellm

uv tool install 'litellm[proxy]' --with prisma
TOOL_DIR="$(uv tool dir)/litellm"
TOOL_BIN="$TOOL_DIR/bin"
TOOL_PY="$TOOL_BIN/python"
export PATH="$TOOL_BIN:$PATH"

"$TOOL_PY" -c "import prisma; print('prisma ok')"

SCHEMA="$("$TOOL_PY" - <<'PY'
import pathlib, litellm
root = pathlib.Path(litellm.__file__).resolve().parent
for p in [
    root / "proxy" / "schema.prisma",
    root.parent / "schema.prisma",
]:
    if p.exists():
        print(p)
        break
PY

)"
"$TOOL_PY" -m prisma generate --schema "$SCHEMA"

systemctl --user enable ./litellm.service ./litellm-caddy.service
systemctl --user daemon-reload
