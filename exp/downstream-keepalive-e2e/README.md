# 下游 SSE 保活 E2E（mock 上游 + 真实 litellm 代理 + 真实 Claude）

验证保活功能打在**真实代理代码路径**上（不是单元 mock），用一个「慢的 mock Anthropic 上游」触发保活，无需真实 github_copilot OAuth——因为 `/v1/messages` 一律走 `route_type=anthropic_messages` → surface=ANTHROPIC → 同一条 `create_response` → normalizer → keepalive 路径。

## 组成

- `mock_upstream.py` — 慢 Anthropic `/v1/messages` 上游（FastAPI/uvicorn）。立即回 200 + `text/event-stream` 头，再 sleep `MOCK_TTFB_SECONDS` 才发首个 SSE 字节（触发面 1 TTFB），可选 `MOCK_GAP_SECONDS` 中途停顿（面 2），随后吐合规 Anthropic 事件流
- `config-on.yaml` / `config-off.yaml` / `config-b-realistic.yaml` — litellm 配置：一个 `anthropic/` deployment 指向 mock，`stream_keepalive` 分别 enabled(interval 3) / disabled / enabled(interval 15)
- `probe.py` — A 档探针：httpx 配**紧 read 超时**打代理，统计 keepalive 帧与真实事件，按 `--expect {survive,timeout}` 返回 PASS/FAIL
- `run-a.sh` — A 档编排：起 mock + 代理(on) 探针→期望 survive；再代理(off) 探针→期望 timeout
- `run-claude.sh [on|off]` — B 档：起 mock(TTFB 330s) + 代理，打印真实 `claude` 启动命令

## A 档结果（已实测 PASS，2026-07-14）

```
=== A-tier: mock TTFB 8s vs client read timeout 5s ===
--- keepalive ON: expect survive ---
  [  7.4s] keepalive comment: ': ping'
  [  9.4s] event: message_start
  ... content_block_start / delta / stop / message_delta / message_stop
result: outcome=survive pings=1 real_events=6 elapsed=9.4s   PASS
--- keepalive OFF: expect timeout ---
result: outcome=timeout pings=0 real_events=0 elapsed=5.1s    PASS
=== A-tier PASS ===
```

解读：keepalive ON 时,代理在 3s 竞速超时后**提前提交 200**（面 1 延迟提交）,头尽早到客户端；随后 `: ping` 注释帧在 8s 首字节之前到达,重置了客户端 5s read 计时器；真实事件在 9.4s 全部到达、流正常结束。keepalive OFF 时,代理走原路径缓冲首 chunk、TTFB 期间不发头,客户端 5s read 超时触发。这正是设计的两面行为对比。

跑：`./run-a.sh`（约 60-90s,含两次代理启动）。可调 `MOCK_TTFB_SECONDS` / `READ_TIMEOUT` / `MOCK_GAP_SECONDS`。

## B 档（真实 Claude Code，手动）

`./run-claude.sh on`（默认,keepalive on/interval 15/mock TTFB 330s）起好 mock + 代理后,按提示在另一终端跑:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 ANTHROPIC_API_KEY=sk-keepalive-test ANTHROPIC_MODEL=test-claude claude
```

发任意消息。用**真实默认超时**（Claude Code `API_FORCE_IDLE_TIMEOUT` 默认 300s,不缩短）:

- keepalive ON：每 15s 一个 ping,Claude 撑过 330s 首字节延迟,收到 "hello from mock"
- keepalive OFF（`./run-claude.sh off`）：Claude 在 ~300s idle 超时中止

代价:每次要等 ~330s 看到差异（用户已确认等得起）。

坑（与保活无关,但影响 B 档跑通）:Claude Code 可能先打 `/v1/messages/count_tokens`（mock 已应答）；模型名经 `ANTHROPIC_MODEL=test-claude` 传入；mock 的 SSE 已 spec 合规。

## 为什么放 exp/ 而非 tests/e2e/

`tests/e2e/` 的 harness（Transport/Gateway/pydantic models）面向 provider/spend/budget 行为、打活代理,不适配「mock 慢上游 + 紧 read 超时」这种保活时序验证；B 档更是手动 `claude` 联调,不属 pytest。故与已有 `exp/downstream-keepalive-timeout/` PoC 放一起。
