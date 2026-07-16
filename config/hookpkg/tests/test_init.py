"""hookpkg/__init__.py 单测:目前聚焦孤儿修复前快照的性能门控 _orphan_pre_fix_snapshot。
运行:python3 -m unittest hookpkg.tests.test_init -v  (从 litellm 根目录)

回归背景:旧 process() 无条件对每请求 `json.loads(json.dumps(messages))` 深拷贝整份历史
(大上下文占 process() 的 71%),只为极罕见的孤儿证据 dump。改为仅当确有孤儿才快照。
本测试两向锁定:干净输入不得快照(否则退回浪费)、有孤儿必须快照(否则丢证据)。"""
import unittest

from hookpkg import _orphan_pre_fix_snapshot


_ORPHAN_MSGS = [
    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    # assistant 的 tool_use t1 之后紧接的 user 没有对应 tool_result -> 孤儿
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
    {"role": "user", "content": [{"type": "text", "text": "next"}]},
]

_CLEAN_MSGS = [
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
]

_ON = {"probe": {"dump_orphans_only": True}}
_OFF = {"probe": {"dump_orphans_only": False}}


class TestOrphanPreFixSnapshot(unittest.TestCase):
    def test_clean_input_no_snapshot(self):
        # 性能门控核心:无孤儿时绝不深拷贝(杀「无条件快照」变异)。
        self.assertIsNone(_orphan_pre_fix_snapshot(_CLEAN_MSGS, _ON))

    def test_orphan_input_snapshots(self):
        # 有孤儿必须快照(杀「永不快照」变异),且内容等于修复前 messages。
        snap = _orphan_pre_fix_snapshot(_ORPHAN_MSGS, _ON)
        self.assertIsNotNone(snap)
        self.assertEqual(snap, _ORPHAN_MSGS)

    def test_snapshot_is_deep_copy(self):
        # 快照必须是独立深拷贝:事后改原 messages 不得影响已取的修复前快照。
        msgs = [dict(m) for m in _ORPHAN_MSGS]
        # 复制内层 content 使其真正可独立变异
        msgs[1] = {"role": "assistant",
                   "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}
        snap = _orphan_pre_fix_snapshot(msgs, _ON)
        msgs[1]["content"].append({"type": "text", "text": "MUTATED AFTER SNAPSHOT"})
        self.assertNotIn({"type": "text", "text": "MUTATED AFTER SNAPSHOT"}, snap[1]["content"])

    def test_toggle_off_no_snapshot(self):
        # dump_orphans_only 关时不快照,即便有孤儿。
        self.assertIsNone(_orphan_pre_fix_snapshot(_ORPHAN_MSGS, _OFF))

    def test_non_list_none(self):
        self.assertIsNone(_orphan_pre_fix_snapshot(None, _ON))
        self.assertIsNone(_orphan_pre_fix_snapshot("nope", _ON))


if __name__ == "__main__":
    unittest.main()
