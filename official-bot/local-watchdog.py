#!/usr/bin/env python3
"""Local zero-token watchdog for official-ws-bridge.py.

Runs every 120s locally:
1. Ensures /root/forwarder/{url,key} exist (restores from persist if missing).
2. Ensures exactly one official-ws-bridge.py process is running (starts run-official-ws.sh if 0, kills older duplicates if >1).
3. Checks official-ws-bridge.log for recent QQ live signals ('ack ok', 'READY', 'RESUMED', 'recv ').
   - If >8.5 min without live signal: sends SIGUSR1 to trigger RESUME.
   - If still dead on next check (>10.5 min): kills bridge PID so run-official-ws.sh restarts it cleanly.
4. Only if bridge fails repeatedly for >15 min or hits persistent auth/token errors, POSTs a
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

DIR = Path("/workspace/qq-bot-persist/official-bot")
PERSIST = Path("/workspace/qq-bot-persist")
FORWARDER = Path("/root/forwarder")
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
            lines = WD_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-1000:]
            WD_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def rd(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def ensure_forwarder_files() -> None:
    FORWARDER.mkdir(parents=True, exist_ok=True)
    for name, src_name in (("url", "forwarder-url"), ("key", "forwarder-key")):
        dst = FORWARDER / name
        src = PERSIST / src_name
        if not dst.exists() and src.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(dst, 0o600)
            wlog(f"restored {dst} from {src}")


def get_bridge_pids() -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", "official-ws-bridge.py"], text=True)
        pids = [int(x) for x in out.strip().splitlines() if x.strip().isdigit()]
        return sorted(pids)
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []


def start_bridge() -> None:
    try:
        import websocket  # noqa: F401
    except ImportError:
        wlog("websocket-client missing, installing...")
        subprocess.run(
            ["sudo", "python3", "-m", "pip", "install", "--break-system-packages", "websocket-client"],
            check=False,
        )
    wlog("starting run-official-ws.sh")
    subprocess.Popen(
        ["setsid", "nohup", "bash", str(DIR / "run-official-ws.sh")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def parse_bridge_log(now: float) -> tuple[float, list[str], list[str]]:
    """Return (age_of_last_live_signal_sec, recent_fatal_lines, tail_lines)."""
    if not BRIDGE_LOG.exists():
        return 999999.0, [], []
    try:
        lines = BRIDGE_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-250:]
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
    if not url.startswith("http") or not key:
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
            keep = pids[-1]
            for p in pids[:-1]:
                try:
                    os.kill(p, signal.SIGTERM)
                    wlog(f"killed duplicate bridge pid={p} (keeping {keep})")
                except Exception:
                    pass
            pids = [keep]

        if not pids:
            wlog("bridge not running! starting...")
            start_bridge()
            time.sleep(6)
            now = time.time()
            pids = get_bridge_pids()
            if not pids:
                age, _, tail = parse_bridge_log(now)
                send_webhook_alert("official-ws-bridge 进程无法拉起", tail, state)
            else:
                wlog(f"bridge started pid={pids[0]}")
            return

        pid = pids[0]
        age, fatals, tail = parse_bridge_log(now)

        if len(fatals) >= 4 and age > 180:
            send_webhook_alert(f"bridge 连续出现鉴权/会话错误 ({len(fatals)} 次)", tail, state)
            return

        if age <= STALE_USR1_SEC:
            state["unhealthy_since"] = 0.0
            wlog(f"ok pid={pid} last_live={int(age)}s ago")
            return

        if vm_woke or not state.get("unhealthy_since"):
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
            STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        except Exception as e:
            wlog(f"check_once error: {e!r}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
