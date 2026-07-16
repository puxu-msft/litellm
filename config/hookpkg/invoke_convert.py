"""泄漏 invoke -> tool_use 转换的解析与合成 / Text-leaked invoke parsing & synthesis.

模型有时把本该是 tool_use 的 <invoke name=...> 吐进 text block。这里提供:解析泄漏
文本成有序片段、白名单(glob)匹配、以及合成 tool_use / text block 的 SSE 事件。
状态机编排在 stream.py。
"""
from __future__ import annotations

import fnmatch
import json
import re

from hookpkg.sse import sse_serialize

_INVOKE_OPEN = re.compile(r'<(?:antml:)?invoke\s+name="([^"]+)"\s*>')
_INVOKE_CLOSE = re.compile(r'</(?:antml:)?invoke\s*>')
_FC_OPEN_TAIL = re.compile(r'<(?:antml:)?function_calls\s*>\s*$')
_FC_CLOSE_HEAD = re.compile(r'^\s*</(?:antml:)?function_calls\s*>')
_PARAM = re.compile(
    r'<(?:antml:)?parameter\s+name="([^"]+)"\s*>(.*?)</(?:antml:)?parameter\s*>', re.S)


def text_maybe_has_invoke(text):
    """便宜的 substring 预判,避免每个 text 都跑正则。"""
    return ("<invoke" in text) or ("antml:invoke" in text)


def extract_invoke_from_text(text):
    """把泄漏了 <invoke> 的 text 切成有序片段序列(支持一个 text 里多个 invoke)。
    返回 [("text", str) | ("tool_use", name, input_dict), ...],或 None(无完整闭合 invoke)。
    text 片段原样保留(含 court 之类残渣——不剔除)。"""
    if not text_maybe_has_invoke(text):
        return None
    segments = []
    pos = 0
    found_any = False
    while True:
        m_inv = _INVOKE_OPEN.search(text, pos)
        if not m_inv:
            break
        m_end = _INVOKE_CLOSE.search(text, m_inv.end())
        if not m_end:
            break  # 未闭合:剩余当作文本,停止切分
        pre = text[pos:m_inv.start()]
        m_fc = _FC_OPEN_TAIL.search(pre)
        if m_fc:
            pre = pre[:m_fc.start()]
        if pre:
            segments.append(("text", pre))
        inner = text[m_inv.end():m_end.start()]
        input_dict = {}
        for pm in _PARAM.finditer(inner):
            input_dict[pm.group(1)] = pm.group(2)
        segments.append(("tool_use", m_inv.group(1), input_dict))
        found_any = True
        pos = m_end.end()
        m_fcc = _FC_CLOSE_HEAD.search(text[pos:])
        if m_fcc:
            pos += m_fcc.end()
    if not found_any:
        return None
    tail = text[pos:]
    if tail:
        segments.append(("text", tail))
    return segments


def name_in_whitelist(name, whitelist):
    """工具名是否匹配白名单。名单项支持 glob 通配(fnmatch):精确名、前缀 mcp__plugin_*、
    单字符 Tool? 等。无通配符的项退化为精确匹配。"""
    if not name:
        return False
    for pat in whitelist:
        if name == pat or fnmatch.fnmatchcase(name, pat):
            return True
    return False


def synth_tool_use_events(index, tool_name, input_dict, template_chunk):
    """为一个合成的 tool_use block 生成 [start, delta, stop] 三个已序列化的 chunk。"""
    # id 必须唯一:同一 text block 里可能有多个同名 invoke,用 index 保证唯一。
    tuid = "toolu_synth_{}_{}".format(re.sub(r'[^a-zA-Z0-9]', '', tool_name)[:16], index)
    start = {"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": tuid, "name": tool_name, "input": {}}}
    delta = {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta",
                       "partial_json": json.dumps(input_dict, ensure_ascii=False)}}
    stop = {"type": "content_block_stop", "index": index}
    return [sse_serialize(start, template_chunk),
            sse_serialize(delta, template_chunk),
            sse_serialize(stop, template_chunk)]


def synth_text_events(index, text, template_chunk):
    """为一段文本生成 [start, delta, stop] 三个已序列化的 chunk。text 为空则返回 []。"""
    if not text:
        return []
    start = {"type": "content_block_start", "index": index,
             "content_block": {"type": "text", "text": ""}}
    delta = {"type": "content_block_delta", "index": index,
             "delta": {"type": "text_delta", "text": text}}
    stop = {"type": "content_block_stop", "index": index}
    return [sse_serialize(start, template_chunk),
            sse_serialize(delta, template_chunk),
            sse_serialize(stop, template_chunk)]


def emit_plain_text_block(index, text, template_chunk):
    """回放一个(可能被缓冲过的)text block。空文本时仍发 start+stop 以保持 block 存在。"""
    if text:
        return synth_text_events(index, text, template_chunk)
    start = {"type": "content_block_start", "index": index,
             "content_block": {"type": "text", "text": ""}}
    stop = {"type": "content_block_stop", "index": index}
    return [sse_serialize(start, template_chunk), sse_serialize(stop, template_chunk)]
