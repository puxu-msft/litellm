"""四类工具参数修复 pipeline / Tool-input fix pipeline.

按意图优先级顺序编排(用户指定):
  1. string-to-array (coerce)  —— 内部借助 json-fix/unicode-fix 把字符串解析成数组
  2. header-to-question (fields) —— 补全 items 内缺失字段

json-fix / unicode-fix 不是独立 pipeline 阶段,而是"把字符串变成结构"的解析容错手段
(在 coerce 内、以及调用方解析顶层 raw 时复用 json_repair.loads_lenient)。这符合
"A 意图优先级"决策:pipeline 声明的是意图顺序,解析容错作为工具被复用。

`apply_item_fixes` 是编排入口,替代旧 hook_impl._apply_item_fixes,行为一致。
"""
from __future__ import annotations

from hookpkg.fixes import coerce, fields

# json_repair / unicode_repair 也导出,供 stream.py 解析顶层 raw 时复用。
from hookpkg.fixes import json_repair, unicode_repair  # noqa: F401


def apply_item_fixes(input_obj, rule):
    """按规则原地修复 input dict。返回是否有改动。

    顺序是**正确性硬依赖,不是偏好排序**:coerce(string->array)必须先于
    fields(header->question)。questions 常被上游双重编码成 JSON 字符串(泄漏路径尤甚),
    若先跑 fields,其 isinstance(items, list) 为 False 会静默跳过 -> question 永远补不上
    (实测反序时 question=None)。coerce 先把字符串还原成 list,fields 才遍历得到 items。
    改此顺序前先看 tests/test_fixes.py 的顺序契约测试——对调两步该测试即红。"""
    if not isinstance(input_obj, dict):
        return False
    items_key = rule.get("items_key")
    changed = False
    # 1) string-to-array(**必须在 fields 之前**,见 docstring 的顺序契约):items_key
    #    本该是数组却成了字符串/单对象时强制还原(可配置)。内部 loads_lenient 复用 json/unicode 修复。
    if items_key and rule.get("coerce_items_json", True):
        if coerce.coerce_items_type(input_obj, items_key):
            changed = True
    # 2) header-to-question:补全 items 内缺失字段(依赖上一步已把 items 还原成 list)。
    if fields.apply_field_fixes(input_obj, rule):
        changed = True
    return changed
