"""header-to-question / 缺字段补全.

按规则从同级字段复制补全 items 内缺失的字段(如 questions[].question 缺则从
header 补)。
"""
from __future__ import annotations


def apply_field_fixes(input_obj, rule):
    """按 copy_within_items 规则补全 items 内缺失字段。返回是否有改动。
    仅当目标字段为空且源字段存在才复制;已有值不动。

    **顺序契约**:假设 input[items_key] 已是 list(由上游 coerce 保证)。若它仍是字符串
    (questions 被双重编码却没先 coerce),下方 isinstance(items, list) 为 False,整体静默
    跳过、什么都不补——所以 apply_item_fixes 里 coerce 必须先跑,见 fixes/__init__.py。"""
    if not isinstance(input_obj, dict):
        return False
    items_key = rule.get("items_key")
    copy_rules = rule.get("copy_within_items") or []
    if not items_key or not copy_rules:
        return False
    changed = False
    items = input_obj.get(items_key)
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            for cr in copy_rules:
                src, dst = cr.get("src"), cr.get("dst")
                if not src or not dst:
                    continue
                if not it.get(dst) and it.get(src):
                    it[dst] = it[src]
                    changed = True
    return changed
