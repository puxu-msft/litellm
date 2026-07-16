#!/usr/bin/env bash
# 触发 litellm hookpkg 热重载:发 SIGUSR2,下次请求时整个包按拓扑顺序 reload。
# 进行中的流式请求不受影响(见 hookpkg/reload.py)。
#
# 发送侧落痕:每次发送写一条到 probe-logs/reload-sends.jsonl(ts + 目标 PID),与
# reload.py 的接收侧审计(reload-audit.jsonl 的 signal_count / reload_ok)对账——
# 「发了但 signal_count 没涨」= 信号没到达;「到了但没 reload_ok」= 轮询/reload 出问题。
#
# ⚠️ 改了 hookpkg/reload.py 本身(含本次埋点)后,SIGUSR2 **重载不了 reloader 自身**,
# 必须整进程重启 litellm 才能让新的 reload.py 生效。
set -euo pipefail

CFG_PORT="${LITELLM_PORT:-4142}"
SENDLOG="$(dirname "$0")/probe-logs/reload-sends.jsonl"

# 优先锁定「真正监听服务端口」的那个 python 进程(避免 head -1 在多实例时选错);
# 拿不到再回退到 pgrep 首个匹配。
PID="$(ss -ltnp 2>/dev/null | awk -v p=":${CFG_PORT}" '$4 ~ p {print}' \
        | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"
if [ -z "${PID:-}" ]; then
  PID="$(pgrep -f "bin/python.*litellm.*--config" | head -1 || true)"
fi
if [ -z "${PID:-}" ]; then
  echo "未找到 litellm 进程(端口 ${CFG_PORT} 无监听、pgrep 也无匹配)" >&2
  exit 1
fi

STARTED="$(ps -o lstart= -p "$PID" 2>/dev/null | sed 's/^ *//' || true)"
TS="$(date +%s.%3N)"
# 发送侧落痕(best-effort,写不进不影响发送)。
printf '{"event":"signal_sent","ts":%s,"target_pid":%s,"port":%s,"sender_pid":%s}\n' \
  "$TS" "$PID" "$CFG_PORT" "$$" >> "$SENDLOG" 2>/dev/null || true

kill -USR2 "$PID"
echo "已发 SIGUSR2 到 litellm PID=${PID}(启动于 ${STARTED:-?},监听 :${CFG_PORT});下次请求时重载 hookpkg"
echo "验证生效:probe-logs/reload-audit.jsonl 应新增一条 reload_ok(pid=${PID}, n_reloaded 对得上);"
echo "         若只见 signal_count 涨而无 reload_ok,或 signal_count 不涨,按 reload.py 顶部口诀排查。"
