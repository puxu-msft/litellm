#!/usr/bin/env bash

brew install postgresql redis
brew services start postgresql
brew services start redis

createdb litellm

pushd "$(dirname "$0")" || exit 1

uv sync --extra proxy --extra extra_proxy

# 检查迁移状态
uv run --no-sync -- prisma migrate status

# 应用仓库中已有的正式迁移
uv run --no-sync -- prisma migrate deploy

# （开发时）直接把数据库同步到当前 schema.prisma
# uv run --no-sync -- prisma db push --skip-generate

# 生成 prisma-client-py
prisma_dir=(.venv/lib/python*/site-packages/prisma)
schema_hash="$(sha256sum schema.prisma | cut -d' ' -f1)"
stamp="${prisma_dir[0]}/.litellm-schema.sha256"
if [[ "$(cat "$stamp" 2>/dev/null)" != "$schema_hash" ]]; then
  uv run --no-sync -- prisma generate
  printf '%s\n' "$schema_hash" > "$stamp"
fi

# systemctl --user enable ./config/litellm.service
systemctl --user enable ./config/litellm-caddy.service
systemctl --user daemon-reload

popd
