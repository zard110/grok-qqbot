"""One lifecycle owner per role; no dependency installation or credential access."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

# Also executable as a file from the legacy shell wrappers.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.files import atomic_write_json, file_lock
from core.settings import DATA_DIR

ROLES = {"bridge": "official-ws-bridge.py", "watchdog": "local-watchdog.py"}


def restart_delay(failures: int) -> float:
    """Bound rapid restart storms while allowing recovery without intervention."""
    return min(300.0, 5.0 * (2 ** min(max(failures - 1, 0), 6)))


def run(role: str, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()
    code = Path(os.environ.get("QQBOT_CODE_DIR", Path(__file__).resolve().parents[1]))
    script = code / ROLES[role]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    health = DATA_DIR / f"supervisor-{role}.json"
    logger = logging.getLogger(f"qqbot.supervisor.{role}")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        DATA_DIR / ("official-ws-bridge.log" if role == "bridge" else "local-watchdog.log"),
        maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    failures = 0
    child: subprocess.Popen | None = None
    reader: threading.Thread | None = None

    def state(status: str, **extra) -> None:
        atomic_write_json(health, {"role": role, "status": status,
            "pid": os.getpid(), "child_pid": child.pid if child else None,
            "updated_at": time.time(), "consecutive_failures": failures, **extra})

    def pump(process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in iter(lambda: process.stdout.readline(65_536), ""):
            logger.info(line.rstrip("\r\n"))

    try:
        with file_lock(DATA_DIR / f"supervisor-{role}.lock", timeout=0):
            try:
                while not stop.is_set():
                    started = time.monotonic()
                    state("starting")
                    child = subprocess.Popen(
                        [sys.executable, "-u", str(script)], cwd=code,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    )
                    reader = threading.Thread(target=pump, args=(child,), daemon=True)
                    reader.start()
                    while child.poll() is None and not stop.is_set():
                        state("running", started_at=time.time() - (time.monotonic() - started))
                        stop.wait(5)
                    if stop.is_set():
                        break
                    reader.join(timeout=2)
                    exit_code = child.returncode
                    if child.stdout:
                        child.stdout.close()
                    child = None
                    failures = 1 if time.monotonic() - started >= 300 else failures + 1
                    delay = restart_delay(failures)
                    state("backoff", exit_code=exit_code, retry_at=time.time() + delay)
                    stop.wait(delay)
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=2)
                if child is not None and child.stdout is not None:
                    child.stdout.close()
                state("stopped")
    except TimeoutError:
        # An already-running supervisor is a successful, idempotent start.
        return 0
    finally:
        logger.removeHandler(handler)
        handler.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=ROLES)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return run(args.role, stop)


if __name__ == "__main__":
    raise SystemExit(main())
