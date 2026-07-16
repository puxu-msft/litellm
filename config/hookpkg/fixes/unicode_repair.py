"""unicode-fix / 坏 unicode 修复.

修复模型产出的坏 unicode 转义(如 \\u 后非十六进制、\\u8k),以及已解码字符串里的
孤立代理码点,使 JSON 可被 json.loads 接受。只做最小损伤修复,不猜原意字符。
"""
from __future__ import annotations

import re

_BAD_U = re.compile(r'\\u(?![0-9a-fA-F]{4})')


def repair_bad_unicode(s):
    """把坏 unicode 转义修成可解析形态。返回修复后的字符串(可能与原串相同)。"""
    if not isinstance(s, str):
        return s
    # 1) 非法 \u 转义:\u 后必须紧跟 4 个十六进制。不满足的(如 \u8k、\u 后是空格)
    #    把反斜杠去掉,退化成字面 "u",使其不再是转义序列。只动未被 \\ 转义的 \u。
    s2 = _BAD_U.sub('u', s)
    # 2) 孤立代理码点(已解码字符串里):无法编码成 utf-8,replace 掉。
    try:
        s2.encode('utf-8')
    except UnicodeEncodeError:
        s2 = s2.encode('utf-8', 'replace').decode('utf-8')
    return s2
