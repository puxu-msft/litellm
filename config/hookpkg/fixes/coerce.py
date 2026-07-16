"""string-to-array / 类型强制.

把本该是数组却成了字符串/单对象的字段(如 AskUserQuestion 的 questions 被双重编码
成 JSON 字符串)强制还原成 list。字符串解析复用 json_repair.loads_lenient(意图优先级:
本阶段负责"变成数组",内部借助 json/unicode 修复作为工具)。
"""
from __future__ import annotations

import logging

from hookpkg.fixes import json_repair

logger = logging.getLogger("litellm.hookpkg.fixes.coerce")


def coerce_items_type(input_obj, items_key):
    """把 input[items_key] 强制还原成 list。返回是否有改动。
    策略:
      - str  -> 宽松解析;list 采用;dict 包成 [dict];标量则放弃(不硬造)。
      - dict -> 包成 [dict](模型可能只输出了单个元素对象)。
      - 其它(已是 list 或无该键) -> 不动。
    """
    if not isinstance(input_obj, dict) or not items_key:
        return False
    val = input_obj.get(items_key)
    if isinstance(val, list):
        return False
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return False
        parsed, repaired = json_repair.loads_lenient(s)
        if parsed is None:
            return False  # 无法安全还原,保持原样
        if repaired:
            logger.warning("stream_fix: repaired truncated JSON for items_key=%r", items_key)
        if isinstance(parsed, list):
            input_obj[items_key] = parsed
            return True
        if isinstance(parsed, dict):
            input_obj[items_key] = [parsed]
            return True
        return False  # 标量(数字/字符串/bool):不硬造数组
    if isinstance(val, dict):
        input_obj[items_key] = [val]
        return True
    return False
