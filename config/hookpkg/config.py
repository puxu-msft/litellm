"""配置热读 / Config hot-read.

从 CONFIG_PATH(默认 hooks.config.json)按 mtime 热读,与包级 SIGUSR2 reload 无关
(配置改动即时生效,无需发信号)。_DEFAULT_CONFIG 提供默认值并对嵌套 dict 深合并。
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("litellm.hookpkg.config")

CONFIG_PATH = os.environ.get(
    "LITELLM_HOOKS_CONFIG", "/home/xp/.config/litellm/hooks.config.json"
)

_DEFAULT_CONFIG = {
    "fix_tool_choice": True,
    "strip_cache_control_scope": True,
    "fix_orphan_tool_use": True,
    # 剥离 copilot 原生 /v1/messages 端点不接受的顶层字段。默认空——注意 context_management
    # 【不该】在此:它是 beta 特性,copilot 完全支持,只需正确透传 anthropic-beta header
    # (context-management-2025-06-27)。误剥会关掉 context editing 能力。
    "strip_unsupported_top_level": [],
    # 按模型剥离特定 anthropic-beta 值。litellm 已默认为 github_copilot 透传所有 beta,
    # 但若发现某 beta 对某模型不被后端支持,可在此按模型(glob)配置剥离对应 beta。
    # 形如 {"claude-opus-4.8": ["some-beta-2025-xx-xx"], "*haiku*": ["other-beta"]}。
    # 剥离作用于 data.provider_specific_header.extra_headers 里的 anthropic-beta。
    "strip_beta_by_model": {},
    # thinking block 修复(请求侧)。客户端可能发回损坏的 thinking 排列。
    "fix_thinking": {
        # 连续 thinking:同一 message 内相邻两个 thinking 之间插空格文本块
        "insert_text": True,
        # 空 signature 的 thinking 处理:"to_text"(转普通文本,内容为空则删)/"remove"(整个删)/"off"
        "empty_signature": "to_text",
        # 一键剥离所有 thinking block(优先级最高,开了就不做上面两项)
        "strip_all": False,
    },
    "probe": {"enabled": False, "file": "/tmp/litellm-toolprobe.jsonl"},
    "inject_tools": {
        "enabled": False,
        "model_contains": "gpt",
        "only_if_tools_present": True,
        "tools": [],
    },
    # 全链路探针体系:各官方 hook 点的可观测性开关。默认关,按需开。
    "deployment_probe": {
        "enabled": False,
        # 转换后检测到孤儿 tool_call 时落盘(转换前后无法对比,但能定位发出前的洞)
        "orphan_only": True,
        "file": "/tmp/litellm-deployment-orphans.jsonl",
        # 无条件 dump 每个转换后载荷(量大,慎用)
        "dump_all": False,
        "dump_file": "/tmp/litellm-deployment.jsonl",
        "model_contains": "",
        # 是否顺手在发出前修复孤儿(把观测点升级为修复点)
        "fix_orphans": False,
    },
    "failure_probe": {
        "enabled": False,
        "file": "/tmp/litellm-failures.jsonl",
        # 仅记录消息里含该子串的失败(如 "tool_result"),留空则记录全部
        "match_exception_contains": "",
    },
    "success_probe": {
        "enabled": False,
        "file": "/tmp/litellm-success.jsonl",
    },
    # 流式响应改写:补全模型漏填的工具参数(如 AskUserQuestion 缺 question)。
    "stream_fix": {
        "enabled": False,
        # 需要补全的工具及规则。key=工具名,value=规则
        # copy_within_items: 每个 items[] 元素若缺 dst 字段,从 src 字段复制
        "tools": {
            "AskUserQuestion": {
                "items_key": "questions",
                "copy_within_items": [{"src": "header", "dst": "question"}],
            }
        },
        # 只读探针:把匹配工具的完整重组 input 落盘,不改写
        "probe_only": False,
        "probe_file": "/home/xp/.config/litellm/probe-logs/stream-tool-input.jsonl",
        # 真正补全时记一笔审计(不含敏感全文,仅工具名),probe_only 关时也记
        "audit_file": "/home/xp/.config/litellm/probe-logs/stream-patched.jsonl",
        # 泄漏转换:把混进 text block 的 <invoke...> 工具调用转成真正的 tool_use block。
        # 涉及 block 拆分、后续 index 偏移、stop_reason 改写。默认关(高风险)。
        "convert_text_invoke": False,
        # 泄漏转换的工具名白名单:非空时仅转这些工具,其余 <invoke> 当普通文本放行
        # (防止误转正常文本里合法提到的 <invoke>)。空=任意 invoke 都转。支持 glob 通配。
        "convert_text_invoke_tools": [],
        # 退化重复裁剪:上游退化时在一个 text block 里连续吐出短小完全相同的片段
        # (如空行分隔的 court×N)。检测并折叠为「首段一次 + notice」。默认关,先灰度。
        # 注意:文本块缓冲已无条件化,stream_fix 常开即全局按 block 成段(非流式);
        # 详见 docs/plan/degeneration-trim.md。
        "degen_trim": {
            "enabled": False,
            # 处理模式:"buffered"=整块缓冲后 fold(默认,已验证);"live"=边流边去重(实时解冻,
            # 保留恢复数据,留 min_run-1 个重复)。live 与 convert_text_invoke 互斥(后者需整块缓冲)。
            "mode": "buffered",
            "min_run": 4,              # \n\n 段落级:连续 ≥4 段相同才判退化
            "max_seg_len": 80,         # \n\n 段落级:仅短段(≤80 字符)参与
            "line_min_run": 6,         # \n 行级回退:更严,连续 ≥6 行
            "line_max_seg_len": 40,    # \n 行级回退:更严,仅 ≤40 字符短行
            "notice": "[上游退化重复输出已被代理裁剪]",  # 不含 \n\n 包裹,不得含 invoke 标记
        },
    },
    # content_block index 生命周期审计(只读)。针对客户端 Content block not found:
    # 双侧观测 stream_transform 的 content_block_start/delta/stop index 序列,检出
    # orphan_delta/orphan_stop/index_gap/start_without_stop/unclosed 五类断裂。
    # 入站干净出站脏 = regressed(stream_fix 改坏铁证)。见 block_audit.py。
    "block_audit": {
        "enabled": False,
        "file": "/home/xp/.config/litellm/probe-logs/block-seq.jsonl",
        # True=仅落有违规的流(修好后应永远为空);False=每条流都落(建基线用)
        "violation_only": False,
    },
    # 相邻重复 tool_use 去重(发给客户端前的最后一层)。丢弃与紧邻前一个 tool_use
    # 「name + 完整 input 字节完全相同」的块,修复「连发两个内容完全相同的工具调用」
    # (如两个相同 AskUserQuestion)。根因无关、安全(input 不同的合法背靠背永不误伤)。
    # 见 dedup.py 与 docs/illformed-fix.md。
    "dedup_tool_use": {
        "enabled": False,
    },
    # 每个模型请求成功/失败后打一行紧凑访问日志到 stdout(见 logline.py)。靠空格分组 + 颜色区分,例:
    #   claude-sonnet-5 ghc/am  ↑338.2KB ↓11.0KB  ↑2+104.7k+534 ↻99%+1% ↓370  3.42s end_turn stream
    # 依次:模型 缩写provider/call_type | 请求/响应字节 | ↑cache创建+cache读+新输入 ↻命中率 ↓输出 | 用时 结束原因 stream。
    "request_log": {
        "enabled": True,
        # 诊断:行尾附加 response_obj 类型与原始 finish_reason,用于坐实活管线形态;确认后可关。
        "diagnose": True,
        # 抑制 uvicorn 的 per-request access log(那条 "POST /v1/messages ... 200 OK"),由本行接管,
        # 避免同一请求两行重复。注意:非模型端点(GET / 等)的 access 行也会一并静默。
        "suppress_uvicorn_access": True,
        # 颜色:"auto"(stdout 是 TTY 才上色)/"always"/"never"。
        "color": "auto",
        # 端到端计时落盘(可选):配了路径才落,每条成功请求追加一行 total/ttft/gen/tokens,
        # 供离线聚合真实端到端耗时分布(见 logline.build_timing_record)。None=不落盘。
        "timing_file": None,
    },
}

_cfg_cache = {"mtime": None, "config": _DEFAULT_CONFIG, "checked_at": 0.0}
_STAT_INTERVAL = 1.0


def load_config():
    now = time.monotonic()
    if now - _cfg_cache["checked_at"] < _STAT_INTERVAL:
        return _cfg_cache["config"]
    _cfg_cache["checked_at"] = now
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        _cfg_cache["mtime"] = None
        _cfg_cache["config"] = _DEFAULT_CONFIG
        return _cfg_cache["config"]
    if mtime == _cfg_cache["mtime"]:
        return _cfg_cache["config"]
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        merged = {**_DEFAULT_CONFIG, **loaded}
        for k in ("probe", "inject_tools", "deployment_probe", "failure_probe", "success_probe", "stream_fix", "fix_thinking", "strip_beta_by_model", "block_audit", "dedup_tool_use", "request_log"):
            if isinstance(loaded.get(k), dict):
                merged[k] = {**_DEFAULT_CONFIG[k], **loaded[k]}
        _cfg_cache["config"] = merged
        _cfg_cache["mtime"] = mtime
        logger.warning("hookpkg: reloaded config from %s", CONFIG_PATH)
    except Exception as e:
        logger.warning("hookpkg: bad config (%r), keeping previous", e)
    return _cfg_cache["config"]


def default_degen_trim():
    """degen_trim 默认值的公开 getter(浅拷贝)。供 stream.py 逐参数兜底,避免跨模块读
    私有 _DEFAULT_CONFIG,也避免默认值在两处 drift(单一真相源)。"""
    return dict(_DEFAULT_CONFIG["stream_fix"]["degen_trim"])

