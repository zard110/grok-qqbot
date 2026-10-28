#!/usr/bin/env bash
# Legacy entry point: exactly one supervisor owns bridge restarts.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${QQBOT_CODE_DIR:-$SCRIPT_DIR}"
if [ ! -f "$CODE_DIR/core/supervisor.py" ]; then
  CODE_DIR=/workspace/qq-bot-persist/official-bot
fi
export QQBOT_CODE_DIR="$CODE_DIR"
export QQBOT_DATA_DIR="${QQBOT_DATA_DIR:-$CODE_DIR}"
exec "${QQBOT_PYTHON:-python3}" "$CODE_DIR/core/supervisor.py" bridge
