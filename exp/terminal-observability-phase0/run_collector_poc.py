from __future__ import annotations

import argparse
import http.client
import json
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, path: str, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return response.read().decode()


def _wait_for_pid(port: int, deadline: float) -> int:
    while time.monotonic() < deadline:
        try:
            return int(_request(port, "/pid"))
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.05)
    raise TimeoutError("uvicorn workers did not become ready")


def _events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _pid_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _wait_for_replacement(port: int, old_pid: int, deadline: float) -> int:
    while time.monotonic() < deadline:
        try:
            current = int(_request(port, "/pid"))
            if current != old_pid:
                return current
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise TimeoutError("uvicorn did not start a replacement worker")


def _run_mode(here: Path, mode: str) -> dict[str, object]:
    sys.stderr.write(f"[collector-poc] starting mode={mode}\n")
    sys.stderr.flush()
    port = _free_port()

    with tempfile.TemporaryDirectory(prefix=f"terminal-collector-{mode}-") as temporary:
        root = Path(temporary)
        event_path = root / "events.jsonl"
        stdout_path = root / "runner.stdout"
        stderr_path = root / "runner.stderr"
        reload_dir = root / "reload"
        reload_dir.mkdir()
        reload_trigger = reload_dir / "trigger.py"
        reload_trigger.write_text("RELOAD_MARKER = 0\n")
        command = [
            sys.executable,
            str(here / "collector_runner.py"),
            "--port",
            str(port),
            "--socket",
            str(root / "collector.sock"),
            "--events",
            str(event_path),
            "--mode",
            mode,
            "--reload-dir",
            str(reload_dir),
        ]
        with stdout_path.open("w+") as stdout_file, stderr_path.open("w+") as stderr_file:
            process = subprocess.Popen(command, cwd=here, stdout=stdout_file, stderr=stderr_file, text=True)
            try:
                first_pid = _wait_for_pid(port, time.monotonic() + 20)
                seen_pids = {first_pid}
                replacement_pid: int | None = None
                if mode == "multiprocess":
                    for _ in range(30):
                        seen_pids.add(int(_request(port, "/pid")))
                        if len(seen_pids) >= 2:
                            break
                    try:
                        _request(port, "/crash")
                    except (urllib.error.URLError, ConnectionError, http.client.RemoteDisconnected):
                        pass
                    replacement_pid = _wait_for_replacement(port, first_pid, time.monotonic() + 20)
                elif mode == "reload":
                    reload_trigger.write_text("RELOAD_MARKER = 1\n")
                    replacement_pid = _wait_for_replacement(port, first_pid, time.monotonic() + 20)

                deadline = time.monotonic() + 20
                expected_started = 3 if mode == "multiprocess" else 2 if mode == "reload" else 1
                while time.monotonic() < deadline:
                    records = _events(event_path)
                    started = {int(record["pid"]) for record in records if record["event"] == "worker_started"}
                    crashed = {int(record["pid"]) for record in records if record["event"] == "worker_crashing"}
                    if len(started) >= expected_started and (mode != "multiprocess" or crashed):
                        break
                    time.sleep(0.05)

                process.send_signal(signal.SIGTERM)
                process.wait(timeout=30)
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
            records = _events(event_path)
            collector_pids = {int(record["collector_pid"]) for record in records}
            worker_pids = {int(record["pid"]) for record in records}
            leaked_pids = sorted(pid for pid in worker_pids if pid != process.pid and _pid_exists(pid))
            started_count = sum(record["event"] == "worker_started" for record in records)
            stopped_count = sum(record["event"] == "worker_stopped" for record in records)
            crashing_count = sum(record["event"] == "worker_crashing" for record in records)
            topology_passed = (
                worker_pids == {process.pid}
                if mode == "direct"
                else process.pid not in worker_pids and replacement_pid in worker_pids
            )
            lifecycle_passed = (
                started_count >= expected_started
                and stopped_count >= (1 if mode == "direct" else 2)
                and (crashing_count >= 1 if mode == "multiprocess" else crashing_count == 0)
            )
            result: dict[str, object] = {
                "mode": mode,
                "passed": (
                    process.returncode == 0
                    and collector_pids == {process.pid}
                    and topology_passed
                    and lifecycle_passed
                    and "Traceback" not in stderr
                    and not leaked_pids
                ),
                "parent_pid": process.pid,
                "collector_pids": sorted(collector_pids),
                "worker_pids": sorted(worker_pids),
                "leaked_worker_pids": leaked_pids,
                "worker_started_count": started_count,
                "worker_stopped_count": stopped_count,
                "worker_crashing_count": crashing_count,
                "returncode": process.returncode,
                "stdout_tail": stdout[-1000:],
                "stderr_tail": stderr[-1000:],
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("direct", "multiprocess", "reload", "all"), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    here = Path(__file__).resolve().parent
    selected_modes = ("direct", "multiprocess", "reload") if args.mode == "all" else (args.mode,)
    runs: list[dict[str, object]] = []
    for repeat_index in range(args.repeat):
        modes = []
        for mode in selected_modes:
            mode_result = _run_mode(here, mode)
            modes.append(mode_result)
            sys.stderr.write(
                f"[collector-poc] repeat={repeat_index + 1}/{args.repeat} "
                f"finished mode={mode} passed={mode_result['passed']}\n"
            )
            sys.stderr.flush()
        runs.append({"repeat": repeat_index + 1, "passed": all(bool(mode["passed"]) for mode in modes), "modes": modes})
    result = {"passed": all(bool(run["passed"]) for run in runs), "repeat_count": args.repeat, "runs": runs}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()