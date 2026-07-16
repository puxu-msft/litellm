"""hookpkg.fixes.apply_item_fixes 单元测试:锁死修复的**顺序契约**与各 fix 行为。
顺序契约是这次 AskUserQuestion `question is missing` 排错的核心结论——coerce(string->array)
必须先于 fields(header->question),否则字符串态的 questions 会让 fields 静默跳过、补不上 question。
运行:python3 -m unittest hookpkg.tests.test_fixes -v (从 litellm 根目录)"""
import unittest

from hookpkg.fixes import apply_item_fixes, coerce, fields

RULE = {"items_key": "questions",
        "copy_within_items": [{"src": "header", "dst": "question"}]}


class TestFixOrderContract(unittest.TestCase):
    """coerce(string->array)必须先于 fields(header->question):正确性硬依赖,非偏好。
    questions 被双重编码成 JSON 字符串时,fields 先跑会 isinstance 检查失败而静默跳过,
    question 永远补不上。下面第一个测试锁死产品代码的顺序——若 fixes/__init__.py 把两步
    对调,question 补不上,断言即红;第二个测试用手动反序固化"为什么不能反"的反例。"""

    def test_stringified_questions_get_question_filled(self):
        """questions 是 JSON 字符串且 item 缺 question:apply_item_fixes 必须既 coerce 成
        数组、又补上 question(=header)。**顺序反了则补不上**——这就是本测试守的产品契约。"""
        inp = {"questions": '[{"header":"Auth method","options":[{"label":"OAuth"}]}]'}
        changed = apply_item_fixes(inp, RULE)
        self.assertTrue(changed)
        self.assertIsInstance(inp["questions"], list, "coerce 必须把字符串还原成数组")
        self.assertEqual(inp["questions"][0]["question"], "Auth method",
                         "coerce 之后 fields 必须补上 question;两步对调则此断言红")

    def test_reversed_order_drops_the_fill(self):
        """反例(可执行注脚):显式按错误顺序手动跑——fields 面对字符串态 questions 必须
        安全跳过(不 crash)且什么都不补,coerce 之后也没人再补 question。固化"为什么
        apply_item_fixes 里 coerce 不能放到 fields 后面"。"""
        inp = {"questions": '[{"header":"Auth method"}]'}
        fields_first = fields.apply_field_fixes(inp, RULE)   # questions 还是 str -> 跳过
        coerce.coerce_items_type(inp, "questions")
        self.assertFalse(fields_first, "fields 面对字符串 questions 必须跳过,不 crash")
        self.assertIsNone(inp["questions"][0].get("question"),
                          "顺序反了时 question 补不上——正是两步不能对调的原因")


class TestFieldFill(unittest.TestCase):
    def test_no_header_cannot_fill_question(self):
        """header 也缺时 question 补不出(缓冲/泄漏两路径共有的盲点,由 stream 层
        tool_out_integrity 探针暴露,兜底策略 deferred)。此处固化当前行为:不硬造。"""
        inp = {"questions": [{"options": []}]}
        self.assertFalse(apply_item_fixes(inp, RULE))
        self.assertNotIn("question", inp["questions"][0])

    def test_existing_question_not_overwritten(self):
        """已有非空 question 不被 header 覆盖(补全只填空,不改已有值)。"""
        inp = {"questions": [{"header": "H", "question": "真问题?"}]}
        apply_item_fixes(inp, RULE)
        self.assertEqual(inp["questions"][0]["question"], "真问题?")


if __name__ == "__main__":
    unittest.main()
