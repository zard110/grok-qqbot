#!/usr/bin/env bash
# Idempotent start; installation is a deployment step, never a recovery action.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${QQBOT_REPO_DIR:-$SCRIPT_DIR}"
if [ ! -f "$REPO_DIR/official-bot/core/supervisor.py" ]; then
  REPO_DIR=/workspace/qq-bot-persist
fi
CODE_DIR="${QQBOT_CODE_DIR:-$REPO_DIR/official-bot}"
DATA_DIR="${QQBOT_DATA_DIR:-$CODE_DIR}"
PYTHON="${QQBOT_PYTHON:-python3}"
export QQBOT_CODE_DIR="$CODE_DIR" QQBOT_DATA_DIR="$DATA_DIR"
mkdir -p -- "$DATA_DIR"
exec 9>"$DATA_DIR/ensure.lock"
flock -n 9 || exit 0
if ! "$PYTHON" -c 'import websocket' >/dev/null 2>&1; then
  echo 'websocket-client missing; install official-bot/requirements.txt before starting.' >&2
  exit 1
fi
for credential in appid appsecret; do
  if [ ! -s "$DATA_DIR/$credential" ]; then
    echo "Missing credential file: $credential" >&2
    exit 1
  fi
done
FORWARDER="${QQBOT_FORWARDER_DIR:-/root/forwarder}"
mkdir -p -- "$FORWARDER"
for name in url key; do
  source="$REPO_DIR/forwarder-$name"
  if [ ! -s "$FORWARDER/$name" ] && [ -s "$source" ]; then
    temporary="$(mktemp "$FORWARDER/.restore.XXXXXX")"
    chmod 600 "$temporary"
    cp -- "$source" "$temporary"
    mv -f -- "$temporary" "$FORWARDER/$name"
  fi
done
# Every invocation is safe: supervisors hold a lifetime role lock, including backoff.
for role in official-ws local-watchdog; do
  nohup bash "$CODE_DIR/run-$role.sh" </dev/null >/dev/null 2>&1 9>&- &
done
# Check supervisor ownership, not only a short-lived Python child.
"$PYTHON" - "$DATA_DIR" <<'PY'
import json, sys, time
from pathlib import Path
root = Path(sys.argv[1])
for _ in range(40):
    healthy = []
    for role in ('bridge', 'watchdog'):
        try:
            state = json.loads((root / f'supervisor-{role}.json').read_text())
            healthy.append(state.get('status') == 'running' and time.time() - state['updated_at'] < 15)
        except (OSError, ValueError, KeyError):
            healthy.append(False)
    if all(healthy):
        print('Supervisors running; QQ connection readiness is reported separately in health status.')
        raise SystemExit(0)
    time.sleep(0.25)
print('Supervisor startup failed; inspect bounded local logs.', file=sys.stderr)
raise SystemExit(1)
PY
