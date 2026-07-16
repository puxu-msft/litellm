"""json-fix / 截断 JSON 修复.

括号栈扫描补齐缺失的尾部 ] } 与未闭合字符串;`loads_lenient` 是三级容错解析:
直接 loads -> 结构修复 -> 坏 unicode 修复 -> 逐步回退。
"""
from __future__ import annotations

import json

from hookpkg.fixes import unicode_repair


def repair_truncated_json(s):
    """启发式修复被截断的 JSON:补齐缺失的尾部 ] } 与未闭合的字符串。
    用轻量括号栈扫描,正确跳过字符串内的括号与转义。只做结构闭合,不猜缺失的值。"""
    if not isinstance(s, str):
        return s
    stack = []          # 未闭合的容器:'[' 或 '{'
    in_str = False
    escaped = False
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '[' or ch == '{':
            stack.append(ch)
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
    if not in_str and not stack:
        return s  # 结构完整,无需修
    repaired = s
    # 未闭合的字符串:先补一个引号闭合它
    if in_str:
        repaired += '"'
    # 去掉尾部残缺(如末尾是 ',' 或 ':' 这种半个键值)
    repaired = repaired.rstrip()
    while repaired and repaired[-1] in ',:':
        repaired = repaired[:-1].rstrip()
    # 按栈逆序补闭合符
    for opener in reversed(stack):
        repaired += ']' if opener == '[' else '}'
    return repaired


def loads_lenient(s):
    """尽力把(可能截断/含坏 unicode 的)字符串解析成 JSON。依次尝试:直接 loads ->
    结构修复(补 ]}) -> 坏 unicode 修复 -> 逐步剥除尾部残缺重试。
    返回 (obj, repaired_bool) 或 (None, False)。"""
    if not isinstance(s, str):
        return None, False
    s = s.strip()
    if not s:
        return None, False
    try:
        return json.loads(s), False
    except Exception:
        pass
    # 尝试结构闭合修复
    rep = repair_truncated_json(s)
    try:
        return json.loads(rep), True
    except Exception:
        pass
    # 坏 unicode 修复(叠加结构修复)
    uni = unicode_repair.repair_bad_unicode(s)
    if uni != s:
        try:
            return json.loads(uni), True
        except Exception:
            pass
        try:
            return json.loads(repair_truncated_json(uni)), True
        except Exception:
            pass
    # 逐步回退:从末尾往前找最后一个可能的元素边界(] } " ),截断后再修复重试
    base = uni if uni != s else s
    for i in range(len(base) - 1, max(0, len(base) - 4000), -1):
        if base[i] in '}]"':
            cand = repair_truncated_json(base[:i + 1])
            try:
                return json.loads(cand), True
            except Exception:
                continue
    return None, False
