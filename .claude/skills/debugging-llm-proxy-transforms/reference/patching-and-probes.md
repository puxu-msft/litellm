# 打 Patch 与探针体系 / Patching & Probes

官方 hook 够不到某一层（如 L2 转换层要对比转换前后）时，才改 site-packages。改动要**可回滚、可重打、与调试探针分离**。

## site-packages patch 工作流

### 生成正确的 patch（对比原始版本）
uv 会在缓存里保留每个版本的原始文件。先确认已装版本，取对应缓存做 diff：
```sh
# 确认版本
ls -d ~/.local/share/uv/tools/litellm/lib/python3.13/site-packages/litellm-*.dist-info
# 找该版本缓存里的原始文件（archive-v0/<hash>/...）
REL="litellm/.../transformation.py"
find ~/.cache/uv/archive-v* -path "*$REL"   # 多个版本，挑与已装版本匹配的 hash
# 生成带 a/ b/ 前缀的 patch（便于 patch -p1）
diff -u "$ORIG" "$CUR" --label "a/$REL" --label "b/$REL" > NN-fix.patch
```

### 分离 fix 与 debug patch
- `01-fix-*.patch` —— 真正的 bug 修复，适合上报上游、升级后重打。
- `02-debug-*.patch` —— 临时调试探针（如对比转换前后），可单独回滚保留 fix。
- 用 `diff` 分两步生成：先造「仅修复」版本，`ORIG→fixonly` 得 patch1，`fixonly→current` 得 patch2。

### 幂等 apply 脚本
```sh
for p in 01-fix.patch 02-debug.patch; do
  if patch -p1 --dry-run --reverse --force <"$p" >/dev/null 2>&1; then
    echo "跳过（已应用）"
  elif patch -p1 --dry-run --forward --force <"$p" >/dev/null 2>&1; then
    patch -p1 --forward <"$p"; echo "已应用"
  else
    echo "警告：无法干净应用（版本可能已变）" >&2
  fi
done
```
`patch -R --dry-run` 判定是否已打；`--forward --dry-run` 判定能否干净打。

### 验证往返一致
从原始版本顺序应用所有 patch，结果应与线上文件**逐字节一致**（`diff -q`）。

### 回滚 / 重装
```sh
patch -R -p1 < 02-debug.patch   # 先撤探针
patch -R -p1 < 01-fix.patch
```
`uv tool upgrade litellm` 会覆盖 site-packages → 重跑 apply.sh。

## 全链路探针体系（官方 hook 版）

`hookpkg` 通过官方 CustomLogger 回调覆盖各层观测点，配置全在 `hooks.config.json`（热读，各探针独立开关）。

```json
{
  "deployment_probe": {           // L3 转换后
    "enabled": true, "orphan_only": true,
    "file": ".../probe-logs/deployment-orphans.jsonl",
    "model_contains": "claude",   // 过滤内部调用噪声
    "fix_orphans": false          // 备用修复开关(默认交给 patch 根治)
  },
  "failure_probe": {              // L6 失败
    "enabled": true,
    "file": ".../probe-logs/failures.jsonl",
    "match_exception_contains": "tool_result"  // 只抓相关失败
  },
  "success_probe": { "enabled": false },  // 流式基本抓不到，默认关
  "stream_fix": { ... }          // L5 见 streaming-rewrite.md
}
```

### 探针设计要点
- **双格式孤儿检测**：L3(转换后)载荷确定是 OpenAI 格式，用 OpenAI 版（`tool_calls`/`role:"tool"`）；L6(失败)/成功后载荷**格式不定**（可能转换前 Anthropic 也可能转换后），用 `_find_orphans_any_format` **两种都探**（内部同时跑 OpenAI 版和 Anthropic 位置感知版，合并标注）。把 L6 写死成单一格式会漏检。
- **重试去重**：`completion_with_retries` 会对同一 `litellm_call_id` 重跑 hook → 用 `_seen_deployment_ids` 集合去重（有上限，溢出清空），只去重 dump 不影响 fix。
- **回归警报**：孤儿探针在修复生效后应**永远为空**。一旦有新行 = patch 失效或新孤儿形态。
- **输出私有化**：探针**应**落 `~/.config/litellm/probe-logs/`（`chmod 700`）。对话含敏感内容，别写 world-readable 的 `/tmp`。注意 `_DEFAULT_CONFIG` 里多数探针默认路径仍是 `/tmp`（仅 `stream_fix.probe_file` 默认 probe-logs），启用时手动改 file 路径。

## 配置合并（热读）

`hookpkg/config.py` 的 `load_config` 对嵌套 dict 键要显式深合并：
```python
for k in ("probe","deployment_probe","failure_probe","success_probe","stream_fix", ...):
    if isinstance(loaded.get(k), dict):
        merged[k] = {**_DEFAULT_CONFIG[k], **loaded[k]}
```
新增一个嵌套配置块时，别忘了把 key 加进这个列表，否则默认值不会与用户配置合并。
