"""包级热重载 / Package hot-reload.

多文件包无法靠单文件 mtime reload。改用 **SIGUSR2 触发**:
- 薄壳导入时注册 handler,收到信号只置一个标志(绝不在 signal 上下文里 reload,避免
  异步重入)。
- 下一次 hook 被调用(新请求)时,`maybe_reload()` 检查标志 -> 按依赖逆序拓扑 reload
  整个包。这样**进行中的流式请求不受影响**(其闭包已绑定旧模块,且只有新请求触发 reload)。

依赖顺序显式声明(见 RELOAD_ORDER),不自动推导,更可控。reload 失败保留旧模块,绝不
打断服务。

PoC 结论见 exp/signal-reload/CONCLUSION.md。

## 诊断埋点(为「SIGUSR2 静默失效」备齐全部信息)

历史故障:`reload.sh` 跑过、请求也过了,但 `reload-audit.jsonl` 里从无一次 `reload_ok`
——即热重载有史以来没成功过一次,部署实际靠整进程重启。可能根因:`signal.signal` 只能在
**主线程**注册(litellm 在 uvicorn/gunicorn 下导入回调模块的时机若不在主线程,注册即静默
降级);也可能是信号被上游框架的 handler 覆盖,或 `maybe_reload` 根本没被轮询到。为一次钉死,
本模块把下列信息全部落盘 `reload-audit.jsonl`(每条自动带运行时上下文,见 `_ctx`):

- **装 handler 时**:PID/TID/线程名/`is_main`(非主线程是头号嫌疑)、注册前后的 SIGUSR2
  处置(`prev_disposition` / `installed_disposition`,用于判断我们的 handler 是否真的生效)。
- **信号到达时**(`_on_signal`,仅改内存不落盘,信号上下文安全):累计 `_signal_count`、时刻、
  PID/TID —— 用于区分「信号没到达」与「到达但没触发 reload」。
- **首次轮询时**(`maybe_reload_first_poll`):证明 `maybe_reload` 确被 hook 入口调用,及其
  线程上下文与**此刻**的 SIGUSR2 处置(检测 handler 被上游框架事后覆盖)。
- **reload 时**:信号→reload 延迟、`signal_count` / `poll_count`,以及既有的模块指纹 mtime。

排查口诀:先看有没有 `handler_installed`(`is_main` 是否 True);再看 `_signal_count`(信号到没到)
与 `maybe_reload_first_poll`(轮询活没活);再看 `reload_ok`(真跑没跑)。三段缺哪段,病就在哪段。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import signal
import threading
import time

logger = logging.getLogger("litellm.hookpkg.reload")

# reload 事件落盘(醒目、持久、可 grep,胜过只落在启动终端的 stdout)。
# 用途:下次若「reload 说成功但新代码没生效」,来这里核对——reload 是否真跑了、
# 卡在哪个模块、以及 reload 当刻磁盘上关键模块的源码 mtime(代码指纹)。
# 注意:probe-logs 在包的**上一级**(与 block-seq.jsonl 等同处),故 dirname 取两层。
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))            # .../litellm/hookpkg
_AUDIT_PATH = os.path.join(os.path.dirname(_PKG_DIR), "probe-logs", "reload-audit.jsonl")
# 记入指纹的关键模块(源文件 mtime 证明 reload 时是哪版代码)。
_FINGERPRINT_MODS = ("block_audit", "stream", "dedup", "config", "invoke_convert", "degen")


def _ctx():
    """当前运行时上下文:PID / TID / 线程名 / 是否主线程 / 主线程 TID。绝不抛错。

    `signal.signal` 只能在主线程注册,`is_main=False` 即注册必失败的头号嫌疑;主线程 TID
    一并记下,便于和信号到达时记录的 TID 对照(CPython 里信号只在主线程被处理)。"""
    try:
        cur = threading.current_thread()
        main = threading.main_thread()
        return {
            "pid": os.getpid(),
            "tid": threading.get_ident(),
            "thread": cur.name,
            "is_main": cur is main,
            "main_tid": main.ident,
        }
    except Exception:
        return {"pid": os.getpid()}


def _sigusr2_disposition():
    """当前 SIGUSR2 的处置对象 repr。用于判断我们的 `_on_signal` 是否仍在位(未被上游
    框架的 handler 覆盖)、或退化为 SIG_DFL/SIG_IGN。绝不抛错。"""
    try:
        h = signal.getsignal(signal.SIGUSR2)
        if h is signal.SIG_DFL:
            return "SIG_DFL"
        if h is signal.SIG_IGN:
            return "SIG_IGN"
        # 具名函数给出模块.限定名,匿名给 repr。判断是否就是我们的 _on_signal。
        name = getattr(h, "__qualname__", None) or repr(h)
        mod = getattr(h, "__module__", None)
        return f"{mod}.{name}" if mod else name
    except Exception as e:
        return f"<getsignal failed: {e!r}>"


def _src_mtimes():
    here = os.path.dirname(os.path.abspath(__file__))
    fp = {}
    for m in _FINGERPRINT_MODS:
        try:
            fp[m] = round(os.path.getmtime(os.path.join(here, m + ".py")), 3)
        except OSError:
            fp[m] = None
    return fp


def _audit(event, **fields):
    """把一次 reload/handler 事件落盘。绝不抛错影响主流程。
    每条自动并入 `_ctx()`(PID/TID/线程),显式传入的同名字段优先。"""
    try:
        rec = {"event": event, "ts": time.time(), **_ctx(), **fields}
        with open(_AUDIT_PATH, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# 拓扑 reload 顺序:被依赖的叶子模块在前,依赖它们的在后。
# reload 时按此顺序逐个 importlib.reload,保证依赖方拿到刷新后的被依赖方。
RELOAD_ORDER = [
    "hookpkg.config",
    "hookpkg.probes",
    "hookpkg.sse",
    "hookpkg.fixes.json_repair",
    "hookpkg.fixes.unicode_repair",
    "hookpkg.fixes.coerce",
    "hookpkg.fixes.fields",
    "hookpkg.fixes",           # PIPELINE 聚合,依赖上面四个
    "hookpkg.invoke_convert",
    "hookpkg.degen",
    "hookpkg.thinking",
    "hookpkg.stream",
    "hookpkg.block_audit",     # 依赖 stream(包装其审计版)
    "hookpkg.dedup",           # 相邻重复 tool_use 去重,链路末端
    "hookpkg.logline",         # 请求访问日志(独立,仅依赖 config)
    "hookpkg",                 # 顶层入口,依赖所有
]

_reload_pending = False
_signal_installed = False

# --- 诊断计数器(全内存,信号上下文安全:_on_signal 只写这些,不做 I/O)---
_signal_count = 0          # SIGUSR2 到达次数(区分「信号没到」与「到了没 reload」)
_last_signal_ts = None     # 最近一次信号到达时刻
_last_signal_tid = None    # 处理该信号的 TID(CPython 恒为主线程,记之以佐证)
_poll_count = 0            # maybe_reload 被调次数(证明 hook 入口在轮询)
_first_poll_audited = False


def _on_signal(signum, frame):
    # 信号上下文:只置标志 + 更新内存计数器,真正 reload 推迟到下次请求(见 maybe_reload)。
    # 绝不在此做文件 I/O / logging(可能与被中断的锁重入死锁)。
    global _reload_pending, _signal_count, _last_signal_ts, _last_signal_tid
    _reload_pending = True
    _signal_count += 1
    _last_signal_ts = time.time()
    try:
        _last_signal_tid = threading.get_ident()
    except Exception:
        _last_signal_tid = None


def install_signal_handler():
    """幂等注册 SIGUSR2。薄壳导入时调用一次。

    落盘 `handler_installed` / `handler_install_failed`,均带 `_ctx()`(尤其 `is_main`)与
    注册前后的信号处置,以便下次一眼判断「装没装上、装在哪个线程、是否被覆盖」。"""
    global _signal_installed
    if _signal_installed:
        return
    ctx = _ctx()
    prev = _sigusr2_disposition()
    if not ctx.get("is_main", False):
        # 明确警示:signal.signal 只在主线程有效;非主线程注册必抛 ValueError。
        logger.warning(">>> hookpkg: install_signal_handler on NON-MAIN thread "
                       "(pid=%s tid=%s thread=%s) — signal.signal will fail",
                       ctx.get("pid"), ctx.get("tid"), ctx.get("thread"))
    try:
        signal.signal(signal.SIGUSR2, _on_signal)
        _signal_installed = True
        installed = _sigusr2_disposition()
        logger.warning(">>> hookpkg: SIGUSR2 reload handler installed "
                       "(pid=%s tid=%s thread=%s is_main=%s prev=%s now=%s)",
                       ctx.get("pid"), ctx.get("tid"), ctx.get("thread"),
                       ctx.get("is_main"), prev, installed)
        _audit("handler_installed", src_mtime=_src_mtimes(), n_modules=len(RELOAD_ORDER),
               prev_disposition=prev, installed_disposition=installed)
    except Exception as e:
        # 非主线程等场景可能无法注册;不致命,退化为无热重载(需重启)。
        logger.warning("hookpkg: cannot install signal handler "
                       "(pid=%s tid=%s thread=%s is_main=%s err=%r); reload via restart",
                       ctx.get("pid"), ctx.get("tid"), ctx.get("thread"), ctx.get("is_main"), e)
        _audit("handler_install_failed", error=repr(e)[:500], prev_disposition=prev)


def maybe_reload():
    """若收到过 SIGUSR2,按拓扑顺序 reload 整个包。在每次 hook 入口调用。
    reload 失败保留旧模块。返回是否发生了 reload。每次 reload 事件落盘 reload-audit.jsonl。"""
    global _reload_pending, _poll_count, _first_poll_audited
    _poll_count += 1
    if not _first_poll_audited:
        # 首次轮询落痕:证明 maybe_reload 确被 hook 入口调用(否则信号来了也永不 reload),
        # 并记此刻 SIGUSR2 处置(检测 handler 被上游框架事后覆盖)。
        _first_poll_audited = True
        logger.warning(">>> hookpkg: maybe_reload first poll "
                       "(pid=%s tid=%s thread=%s is_main=%s sigusr2=%s)",
                       os.getpid(), threading.get_ident(), threading.current_thread().name,
                       threading.current_thread() is threading.main_thread(),
                       _sigusr2_disposition())
        _audit("maybe_reload_first_poll", installed=_signal_installed,
               sigusr2_disposition=_sigusr2_disposition())
    if not _reload_pending:
        return False
    _reload_pending = False
    # 信号→reload 延迟:若很大,说明信号到达后隔了很久才有请求触发轮询。
    sig_latency = (time.time() - _last_signal_ts) if _last_signal_ts is not None else None
    reloaded = []
    current = None                      # 正在处理的模块(抛错时即为失败点)
    try:
        import sys
        for name in RELOAD_ORDER:
            current = name
            mod = sys.modules.get(name)
            if mod is not None:
                importlib.reload(mod)
                reloaded.append(name)
        # 醒目成功日志:N/期望 都打出,偏少一眼可见。
        logger.warning(">>> hookpkg: RELOAD OK — reloaded %d/%d modules via SIGUSR2 "
                       "(pid=%s signal_count=%s poll_count=%s sig_latency=%.3fs)",
                       len(reloaded), len(RELOAD_ORDER), os.getpid(),
                       _signal_count, _poll_count, sig_latency if sig_latency is not None else -1)
        _audit("reload_ok", n_reloaded=len(reloaded), expected=len(RELOAD_ORDER),
               reloaded=reloaded, src_mtime=_src_mtimes(),
               signal_count=_signal_count, poll_count=_poll_count,
               last_signal_ts=_last_signal_ts, last_signal_tid=_last_signal_tid,
               sig_latency=sig_latency)
        return True
    except Exception as e:
        # 醒目失败日志:明确指出卡在哪个模块——该模块及其后全部保留旧代码。
        logger.warning(">>> hookpkg: RELOAD FAILED at %r (%r); "
                       "reloaded only %d/%d, rest keep OLD code (pid=%s)",
                       current, e, len(reloaded), len(RELOAD_ORDER), os.getpid())
        _audit("reload_failed", failed_at=current, error=repr(e)[:800],
               n_reloaded=len(reloaded), expected=len(RELOAD_ORDER),
               reloaded=reloaded, src_mtime=_src_mtimes(),
               signal_count=_signal_count, poll_count=_poll_count,
               last_signal_ts=_last_signal_ts, last_signal_tid=_last_signal_tid,
               sig_latency=sig_latency)
        return False
