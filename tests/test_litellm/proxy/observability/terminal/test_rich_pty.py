from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
from pathlib import Path

import pyte


def _capture_driver() -> tuple[list[str], str]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 80, 0, 0))
    code = """
import time
from rich.console import Console
from litellm.proxy.observability.terminal.render.rich_renderer import RichLiveRenderer
console=Console(file=__import__('sys').stdout,force_terminal=True,width=80,height=10)
renderer=RichLiveRenderer(console,refresh_hz=20)
console.print('\\n'*8,end='')
renderer.start('[ .. ] 2 in-flight  anthropic/opus@ghc ×2 1.00s')
renderer.log('[ OK ] 17:18:53 ■ 7K3M anthropic/opus@ghc 200 8.00s tool_use(Bash,Bash,Read)')
renderer.update('[ .. ] 1 in-flight  anthropic/opus@ghc 2.00s')
time.sleep(.1)
print('---SNAPSHOT---',flush=True)
time.sleep(.1)
renderer.stop()
print('---STOPPED---',flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=Path(__file__).resolve().parents[5],
        env={**os.environ, "TERM": "xterm-256color"},
        close_fds=True,
    )
    os.close(slave)
    screen = pyte.Screen(80, 10)
    stream = pyte.ByteStream(screen)
    raw = b""
    while process.poll() is None:
        readable, _, _ = select.select([master], [], [], 1)
        if readable:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            raw += chunk
            stream.feed(chunk)
    try:
        while True:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            raw += chunk
            stream.feed(chunk)
    except OSError:
        pass
    os.close(master)
    process.wait(timeout=5)
    assert process.returncode == 0, raw.decode(errors="replace")
    return list(screen.display), raw.decode(errors="replace")


def test_rich_live_real_pty_preserves_log_and_clears_footer() -> None:
    display, raw = _capture_driver()
    assert "tool_use(Bash,Bash,Read)" in raw
    assert "1 in-flight" in raw
    assert "---STOPPED---" in "\n".join(display)
    assert all("in-flight" not in line for line in display)
