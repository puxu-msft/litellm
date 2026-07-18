"""pty + pyte 整屏验证 _RichLiveDisplay(实际在用的 Rich Live footer)。

字节级单测(test_logline.TestLiveDisplay)只覆盖旧的 _LiveDisplay,且证不了「完成行会不会被
底部 footer 吞掉」这类整屏效果。这里在真 PTY 里驱真 _RichLiveDisplay,用 pyte HistoryScreen
把输出解释成网格+scrollback,断言:
  1. N 条编号完成行(FOOTLOG-0001..N)全部出现在 grid+scrollback,无缺号(footer 不吞行)
  2. 退出后末屏无 in-flight footer 残留(Rich transient Live 干净还原)

时序相关,连跑多次证确定性。运行:
  cd config && PYTHONPATH=. ../.venv/bin/python -m unittest hookpkg.tests.test_rich_footer_pty -v
"""
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
import unittest

import pyte

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_DIR = os.path.dirname(os.path.dirname(_HERE))              # .../config
_VENV_PY = os.path.join(os.path.dirname(_CONFIG_DIR), ".venv", "bin", "python")
_DRIVER = os.path.join(_HERE, "_rich_footer_driver.py")


def _run_driver(n: int, rows: int = 24, cols: int = 100, timeout: float = 25.0) -> pyte.HistoryScreen:
    """在 pty 里跑 driver,喂输出给 HistoryScreen,返回抓屏结果。"""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ, FOOTER_LOGS=str(n), PYTHONPATH=_CONFIG_DIR, FORCE_COLOR="1")
    proc = subprocess.Popen(
        [_VENV_PY, _DRIVER], stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env,
    )
    os.close(slave)
    screen = pyte.HistoryScreen(cols, rows, history=8000, ratio=0.5)
    stream = pyte.ByteStream(screen)
    start = time.time()
    while True:
        if proc.poll() is not None:
            while True:
                r, _, _ = select.select([master], [], [], 0.2)
                if not r:
                    break
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                stream.feed(data)
            break
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if data:
                stream.feed(data)
        if time.time() - start > timeout:
            break
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    os.close(master)
    return screen


def _row_text(row, cols: int) -> str:
    try:
        return "".join(row[c].data if c in row else " " for c in range(cols)).rstrip()
    except Exception:
        return ""


def _all_text(screen: pyte.HistoryScreen, cols: int) -> str:
    top = "\n".join(_row_text(r, cols) for r in screen.history.top)
    cur = "\n".join(screen.display)
    return top + "\n" + cur


@unittest.skipUnless(os.path.exists(_VENV_PY), "需要 litellm venv 才能 import RichLiveRenderer")
class TestRichFooterPty(unittest.TestCase):
    def test_no_completion_line_eaten_by_footer(self):
        n, cols = 40, 100
        for attempt in range(3):  # 时序相关,连跑证确定性
            screen = _run_driver(n, cols=cols)
            text = _all_text(screen, cols)
            found = {int(m) for m in re.findall(r"FOOTLOG-(\d{4})", text)}
            missing = sorted(set(range(1, n + 1)) - found)
            self.assertEqual(missing, [], f"第{attempt + 1}次:footer 吞了完成行 {missing[:20]}")

    def test_exit_leaves_no_footer_residue(self):
        screen = _run_driver(5, cols=100)
        current = "\n".join(screen.display)
        # Rich transient Live 退出时清除 footer(在途栏含 'in-flight'),末屏不应残留
        self.assertNotIn("in-flight", current)


if __name__ == "__main__":
    unittest.main()
