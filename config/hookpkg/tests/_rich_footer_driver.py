"""在 PTY 内驱**真** _RichLiveDisplay:常驻一个在途请求(footer 活跃),逐条 emit 编号完成行,
跑完 discard+close 干净还原(Rich transient Live 清除 footer)。供 test_rich_footer_pty.py 用
pyte 做整屏断言(完成行不被 footer 吞)。读 env FOOTER_LOGS=条数。仅测试用,不启任何服务。"""
import os
import sys

# tests/ 上两级是 config/(hookpkg 在其下),加入 path 以 import 真实实现
_CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CONFIG_DIR not in sys.path:
    sys.path.insert(0, _CONFIG_DIR)

from hookpkg.logline import _RichLiveDisplay  # noqa: E402


def main() -> None:
    n = int(os.environ.get("FOOTER_LOGS", "40"))
    display = _RichLiveDisplay(stream=sys.stdout, auto_refresh=False)
    # 常驻在途 -> footer 保持活跃,完成行须 print 到其上方滚动区(不能覆盖/吞掉)
    display.start("keep", "am/claude-opus-4.8", started_at=0.0)
    for i in range(1, n + 1):
        display.finish_and_emit(f"done-{i}", f"FOOTLOG-{i:04d} claude-sonnet-5 end_turn")
    display.discard("keep")  # 无在途 -> Rich Live stop,footer 清除
    display.close()
    sys.stdout.flush()


if __name__ == "__main__":
    main()
