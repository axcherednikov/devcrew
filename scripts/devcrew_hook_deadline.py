"""Bound trusted hook execution even when a pipe or another process holds a lock."""

import json
import os
import signal
import subprocess
import sys


def run(main, script, timeout_result):
    if os.name == "nt":
        if sys.argv[1:] == ["--hook-worker"]:
            main()
            return
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", script, "--hook-worker"],
                stdout=subprocess.PIPE,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            print(json.dumps(timeout_result))
            return
        sys.stdout.buffer.write(result.stdout)
        raise SystemExit(result.returncode)

    def expired(_signum, _frame):
        raise TimeoutError("hook deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(3)
    try:
        main()
    finally:
        signal.alarm(0)
