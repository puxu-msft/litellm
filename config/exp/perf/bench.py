"""hookpkg 请求处理时间性能离线基准 / Offline latency benchmark.

用法:  .venv/bin/python exp/perf/bench.py         (从 ~/.config/litellm 跑,或任意 cwd)

测三件事:
  1) 请求侧 process() 总耗时 + 逐子步拆分(重点坐实每请求都做的整份 messages
     JSON round-trip 快照、cache_control 递归 walk、孤儿扫描、thinking 修复)。
  2) 响应侧流式管线逐 chunk CPU,按层拆:纯状态机 / +block_audit / 全链路(含 dedup),
     层间 wall-time 差即各层边际成本(含 block_audit 每 chunk 的冗余 sse_parse)。
  3) cProfile 对大 payload 的 process() 做函数级归因,交叉验证子步拆分。

配置:用 LITELLM_HOOKS_CONFIG 指向一份镜像生产开关、但把落盘路径重定向到 /tmp 的临时
config(不污染真实 probe-logs,也不改真实 config)。
"""
from __future__ import annotations

import asyncio
import cProfile
import io
import json
import logging
import os
import pstats
import sys
import time
from copy import deepcopy
from statistics import median

# hook 内部有大量 logger.warning(修复动作),基准里会刷屏 —— 静默 WARNING 及以下。
logging.disable(logging.WARNING)

_HOOK_ROOT = "/home/xp/.config/litellm"
_REAL_CONFIG = os.path.join(_HOOK_ROOT, "hooks.config.json")


def _prep_bench_config():
    """镜像真实 config 的开关,把所有落盘路径改到临时目录,写到 /tmp 并设 env。"""
    with open(_REAL_CONFIG) as f:
        cfg = json.load(f)
    tmpdir = "/tmp/hookpkg-bench"
    os.makedirs(tmpdir, exist_ok=True)
    # 重定向所有已知的文件落盘键到临时目录(保留 enabled 等开关不变)。
    for section, keys in {
        "probe": ("file", "dump_file", "orphan_dump_file", "toolref_file"),
        "deployment_probe": ("file", "dump_file"),
        "failure_probe": ("file",),
        "success_probe": ("file",),
        "stream_fix": ("probe_file", "audit_file"),
        "block_audit": ("file",),
    }.items():
        if isinstance(cfg.get(section), dict):
            for k in keys:
                if k in cfg[section]:
                    cfg[section][k] = os.path.join(tmpdir, f"{section}-{k}.jsonl")
    path = os.path.join(tmpdir, "bench-config.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    os.environ["LITELLM_HOOKS_CONFIG"] = path
    return cfg


_prep_bench_config()
if _HOOK_ROOT not in sys.path:
    sys.path.insert(0, _HOOK_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hookpkg  # noqa: E402
import hookpkg as hk  # noqa: E402  (私有子步函数经模块属性访问)
from hookpkg.thinking import fix_thinking_blocks  # noqa: E402
from hookpkg.config import load_config  # noqa: E402
from hookpkg import stream as stream_mod  # noqa: E402
from hookpkg import block_audit as ba_mod  # noqa: E402
import synth  # noqa: E402  (同目录)


def _timeit(fn, iters):
    """返回 (median_ms, min_ms) over iters 次,每次独立计时。"""
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return median(samples), min(samples)


def _timeit_prepared(prepare, call, iters):
    """每次先 prepare()(不计时)拿到新参数,再只对 call(arg) 计时。
    用于会原地改参数的子步:deepcopy 成本移出计时区,得到纯函数耗时。"""
    samples = []
    for _ in range(iters):
        arg = prepare()
        t0 = time.perf_counter()
        call(arg)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return median(samples), min(samples)


def bench_request_side(iters=30):
    print("\n" + "=" * 88)
    print("请求侧 process() —— 每请求一次的 hook CPU 成本(生产配置)")
    print("=" * 88)
    pls = synth.payloads()
    cfg = load_config()
    for name in ("small", "medium", "large"):
        info = pls[name]
        data = info["data"]
        kb = info["bytes"] / 1024.0
        # 总耗时:deepcopy 移出计时区(process 会原地改,故每次需新副本,但深拷贝成本
        # 不该算进 hook —— 真实链路里 data 由 litellm 现成传入,无需拷贝)。
        med, mn = _timeit_prepared(lambda: deepcopy(data),
                                   lambda d: hookpkg.process(d, "anthropic_messages"), iters)
        print(f"\n[{name}]  ~{kb:.0f} KB,  {info['n_msgs']} messages")
        print(f"  process() 总计            median={med:8.3f} ms   min={mn:8.3f} ms")

        # 逐子步标准化独立计时。deepcopy 移出计时区(_timeit_prepared),得纯函数耗时。
        msgs = data["messages"]
        snap = _timeit(lambda: json.loads(json.dumps(msgs, default=str)), iters)
        strip = _timeit_prepared(lambda: deepcopy(data),
                                 lambda d: hk._strip_cache_control_scope(d, cfg), iters)
        orph = _timeit_prepared(lambda: deepcopy(data),
                                lambda d: hk._fix_orphan_tool_use(d, cfg), iters)
        think = _timeit_prepared(lambda: deepcopy(data),
                                 lambda d: fix_thinking_blocks(d, cfg), iters)
        beta = _timeit_prepared(lambda: deepcopy(data),
                                lambda d: hk._strip_beta_by_model(d, cfg), iters)
        for label, (m, _mn) in (
            ("  ·  快照若付的成本(现已门控:仅有孤儿才付,不在常态 process)", snap),
            ("  ├─ _strip_cache_control_scope 递归 walk", strip),
            ("  ├─ _fix_orphan_tool_use 扫描", orph),
            ("  ├─ fix_thinking_blocks", think),
            ("  └─ _strip_beta_by_model", beta),
        ):
            pct = 100.0 * m / med if med else 0
            print(f"  {label:<48} median={m:8.3f} ms  ({pct:4.1f}% of process)")


def _drain(agen_factory):
    """驱动一个 async 生成器工厂到底,返回 (wall_ms, n_out_chunks)。"""
    async def run():
        n = 0
        t0 = time.perf_counter()
        async for _ in agen_factory():
            n += 1
        return (time.perf_counter() - t0) * 1000.0, n
    return asyncio.run(run())


def _replay(chunks):
    """把 list[bytes] 变成 async 生成器(每次调用一个新实例)。"""
    async def gen():
        for c in chunks:
            yield c
    return gen


def bench_stream_side(iters=20):
    print("\n" + "=" * 88)
    print("响应侧流式管线 —— 逐 chunk CPU,按层拆分(生产配置)")
    print("=" * 88)
    chunks = synth.build_sse_stream(n_text_deltas=400, tool_input={"file_path": "/repo/x.py"},
                                    n_tool_deltas=6)
    n_in = len(chunks)
    rd = {"model": "github_copilot/claude-opus-4.8", "litellm_call_id": "bench"}
    print(f"\n输入流: {n_in} 个 SSE chunk(400 text_delta + 1 tool_use 块)")

    def layer_state_machine():
        return stream_mod.stream_transform(_replay(chunks)(), rd)

    def layer_audited():
        return ba_mod.stream_transform_audited(_replay(chunks)(), rd)

    def layer_full():
        return hookpkg.stream_transform(_replay(chunks)(), rd)

    results = {}
    for label, fac in (("纯状态机 stream.stream_transform", layer_state_machine),
                       ("+block_audit(双侧观测+落盘)", layer_audited),
                       ("全链路(+dedup)", layer_full)):
        samples = [_drain(fac)[0] for _ in range(iters)]
        wall = median(samples)
        results[label] = wall
        per_chunk = wall / n_in * 1000.0  # µs/chunk
        print(f"  {label:<40} median={wall:7.3f} ms   ({per_chunk:6.1f} µs/chunk)")

    keys = list(results)
    ba_cost = results[keys[1]] - results[keys[0]]
    dd_cost = results[keys[2]] - results[keys[1]]
    print(f"\n  → block_audit 边际(冗余 sse_parse×2/chunk + 每流一次落盘): +{ba_cost:.3f} ms")
    print(f"  → dedup 边际(sse_parse×1/chunk):                         +{dd_cost:.3f} ms")


def profile_large():
    print("\n" + "=" * 88)
    print("cProfile —— 大 payload process() ×50 的函数级归因(交叉验证)")
    print("=" * 88)
    data = synth.payloads()["large"]["data"]
    copies = [deepcopy(data) for _ in range(50)]  # 预建副本,不让 deepcopy 污染 profile
    pr = cProfile.Profile()
    pr.enable()
    for d in copies:
        hookpkg.process(d, "anthropic_messages")
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(18)
    # 只打印函数级前几行(去掉 deepcopy 噪声由读者判断)
    print(s.getvalue())


if __name__ == "__main__":
    print("hookpkg 请求处理时间性能基准 / offline (synthetic payloads)")
    print(f"config: {os.environ['LITELLM_HOOKS_CONFIG']}")
    bench_request_side()
    bench_stream_side()
    profile_large()
