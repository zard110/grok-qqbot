#!/usr/bin/env python3
"""Local zero-token watchdog for official-ws-bridge.py.

Runs every 120s locally:
1. Ensures /root/forwarder/{url,key} exist (restores from persist if missing).
2. Requests an idempotent supervisor start if the bridge is missing; never kills unrelated duplicate processes.
3. Checks official-ws-bridge.log for recent QQ live signals ('ack ok', 'READY', 'RESUMED', 'recv ').
   - If >8.5 min without live signal: sends SIGUSR1 to trigger RESUME.
   - If still dead on next check (>10.5 min): kills bridge PID so run-official-ws.sh restarts it cleanly.
4. Only if bridge fails repeatedly for >15 min, POSTs a
   {"post_type": "watchdog_alert", ...} payload to the Grok webhook (rate-limited to once per 30 min)
   to wake the cloud agent and notify the user.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from core.files import atomic_write_json, atomic_write_text, file_lock
from core.settings import DATA_DIR

DIR = DATA_DIR
CODE_DIR = Path(os.environ.get("QQBOT_CODE_DIR", Path(__file__).resolve().parent))
PERSIST = Path(os.environ.get("QQBOT_REPO_DIR", CODE_DIR.parent))
FORWARDER = Path(os.environ.get("QQBOT_FORWARDER_DIR", "/root/forwarder"))
BRIDGE_LOG = DIR / "official-ws-bridge.log"
WD_LOG = DIR / "local-watchdog.log"
STATE_FILE = DIR / "local-watchdog-state.json"

CHECK_INTERVAL = 120       # seconds
STALE_USR1_SEC = 510       # 8.5 minutes without ack/READY/RESUMED/recv -> send SIGUSR1
STALE_KILL_SEC = 660       # 11 minutes without live signal -> kill PID (run-official-ws.sh respawns)
ALERT_FAIL_SEC = 900       # 15 minutes still broken -> fire webhook alert
ALERT_COOLDOWN_SEC = 1800  # max 1 webhook alert per 30 minutes

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")
LIVE_MARKERS = ("ack ok", "READY ", "RESUMED ", "recv ")
FATAL_MARKERS = ("no access_token", "bad gateway", "INVALID SESSION", "HTTP Error 401", "HTTP Error 403")


def wlog(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}\n"
    try:
        with WD_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
        # Keep log bounded (~200KB)
        if WD_LOG.stat().st_size > 250_000:
            lines = tail_lines(WD_LOG, 1000)
            atomic_write_text(WD_LOG, "\n".join(lines) + "\n")
    except Exception:
        pass


def rd(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def tail_lines(path: Path, count: int, max_bytes: int = 131072) -> list[str]:
    """Read a bounded suffix; slicing after read_text is not bounded I/O."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - max_bytes))
        return stream.read(max_bytes).decode("utf-8", errors="replace").splitlines()[-count:]


def ensure_forwarder_files() -> None:
    FORWARDER.mkdir(parents=True, exist_ok=True)
    for name, src_name in (("url", "forwarder-url"), ("key", "forwarder-key")):
        dst = FORWARDER / name
        src = PERSIST / src_name
        if not dst.exists() and src.exists():
            atomic_write_text(dst, src.read_text(encoding="utf-8"))
            os.chmod(dst, 0o600)
            wlog(f"restored {dst} from {src}")


def get_bridge_pids() -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", str(CODE_DIR / "official-ws-bridge.py")], text=True)
        pids = [int(x) for x in out.strip().splitlines() if x.strip().isdigit()]
        return sorted(pids)
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []


def start_bridge() -> None:
    wlog("starting run-official-ws.sh")
    subprocess.Popen(
        ["bash", str(CODE_DIR / "run-official-ws.sh")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def parse_bridge_log(now: float) -> tuple[float, list[str], list[str]]:
    """Return (age_of_last_live_signal_sec, recent_fatal_lines, tail_lines)."""
    if not BRIDGE_LOG.exists():
        return 999999.0, [], []
    try:
        lines = tail_lines(BRIDGE_LOG, 250)
    except Exception:
        return 999999.0, [], []

    last_live_ts = 0.0
    fatals: list[str] = []
    for ln in lines:
        m = TS_RE.match(ln)
        if not m:
            continue
        ts_str, body = m.group(1), m.group(2)
        try:
            t = time.mktime(datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timetuple())
        except Exception:
            continue
        if any(k in body for k in LIVE_MARKERS):
            if t > last_live_ts:
                last_live_ts = t
        if now - t < 600 and any(fk in body for fk in FATAL_MARKERS):
            fatals.append(ln)

    age = (now - last_live_ts) if last_live_ts > 0 else 999999.0
    return age, fatals, lines[-20:]


def send_webhook_alert(reason: str, tail_lines: list[str], state: dict) -> None:
    now = time.time()
    last_alert = state.get("last_alert_ts", 0.0)
    if now - last_alert < ALERT_COOLDOWN_SEC:
        wlog(f"alert suppressed by cooldown ({int(now - last_alert)}s): {reason}")
        return
    url = rd(FORWARDER / "url") or rd(PERSIST / "forwarder-url")
    key = rd(FORWARDER / "key") or rd(PERSIST / "forwarder-key")
    if not url.startswith("https://") or not key:
        wlog(f"cannot send webhook alert (url/key missing): {reason}")
        return
    payload = {
        "post_type": "watchdog_alert",
        "reason": reason,
        "timestamp": time.strftime("%F %T"),
        "log_tail": "\n".join(tail_lines[-15:]),
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            wlog(f"webhook alert sent ({r.status}): {reason}")
        state["last_alert_ts"] = now
    except Exception as e:
        wlog(f"webhook alert failed: {e}")


def check_once(state: dict) -> None:
    now = time.time()
    last_check_ts = float(state.get("last_check_ts", 0.0) or 0.0)
    vm_woke = False
    try:
        ensure_forwarder_files()
        if last_check_ts > 0 and (now - last_check_ts) > (CHECK_INTERVAL + 90):
            vm_woke = True
            wlog(
                f"VM wake detected (loop gap {int(now - last_check_ts)}s); "
                "waiting 25s for bridge auto-RESUME before checking staleness"
            )
            time.sleep(25)
            now = time.time()

        pids = get_bridge_pids()

        if len(pids) > 1:
            state["gateway_status"] = "duplicate_processes"
            keep = pids[-1]
            stale_pids = pids[:-1]
            wlog(f"killing duplicate bridge pids={stale_pids}, keeping {keep}")
            for sp in stale_pids:
                try:
                    os.kill(sp, signal.SIGTERM)
                except Exception:
                    pass
            pids = [keep]

        if not pids:
            state["gateway_status"] = "missing"
            if not state.get("unhealthy_since"):
                state["unhealthy_since"] = now
            wlog("bridge not running! starting...")
            start_bridge()
            time.sleep(6)
            now = time.time()
            pids = get_bridge_pids()
            if not pids:
                age, _, tail = parse_bridge_log(now)
                if now - state["unhealthy_since"] >= ALERT_FAIL_SEC:
                    send_webhook_alert("official-ws-bridge 进程持续无法拉起", tail, state)
            else:
                wlog(f"bridge started pid={pids[0]}")
            return

        pid = pids[0]
        age, fatals, tail = parse_bridge_log(now)
        state["bridge_pid"] = pid
        state["last_live_age"] = age
        state["gateway_status"] = "healthy" if age <= STALE_USR1_SEC else "stale"
        # A connected gateway may coexist with a broken business delivery path.
        try:
            from core.delivery import snapshot
            state["delivery"] = snapshot()
        except Exception as exc:
            state["delivery"] = {"status": "unavailable", "error_type": type(exc).__name__}

        if len(fatals) >= 4 and age > 180:
            if not state.get("unhealthy_since"):
                state["unhealthy_since"] = now
            if now - state["unhealthy_since"] >= ALERT_FAIL_SEC:
                send_webhook_alert(f"bridge 持续鉴权/会话错误 ({len(fatals)} 次)", tail, state)
            return

        if age <= STALE_USR1_SEC:
            state["unhealthy_since"] = 0.0
            wlog(f"ok pid={pid} last_live={int(age)}s ago")
            return

        if not state.get("unhealthy_since"):
            state["unhealthy_since"] = now

        unhealthy_dur = now - state["unhealthy_since"]

        if age < STALE_KILL_SEC or vm_woke:
            reason_tag = "first check after VM wake" if vm_woke else f"> {STALE_USR1_SEC}s"
            wlog(f"stale live signal ({int(age)}s {reason_tag}), sending SIGUSR1 to pid={pid}")
            try:
                os.kill(pid, signal.SIGUSR1)
            except Exception as e:
                wlog(f"SIGUSR1 failed: {e}")
        else:
            wlog(f"still stale ({int(age)}s >= {STALE_KILL_SEC}s), killing pid={pid} so wrapper restarts it")
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                wlog(f"SIGTERM failed: {e}")

        if unhealthy_dur >= ALERT_FAIL_SEC:
            send_webhook_alert(
                f"official-ws-bridge 已连续 {int(unhealthy_dur)}s 无 QQ 响应，本地 USR1/重启均未恢复",
                tail,
                state,
            )
    finally:
        state["last_check_ts"] = now


def main() -> None:
    state: dict = {}
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    wlog("local-watchdog started")
    while True:
        try:
            check_once(state)
            atomic_write_json(STATE_FILE, state)
        except Exception as e:
            wlog(f"check_once error: {e!r}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    DIR.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(DIR / "watchdog.process.lock", timeout=0):
            main()
    except TimeoutError:
        raise SystemExit("watchdog already running")
