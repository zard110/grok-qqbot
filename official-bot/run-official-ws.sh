#!/bin/bash
# Official MaxShenBi gateway bridge (persistent). Prefer this over NapCat ws-bridge.
DIR=/workspace/qq-bot-persist/official-bot
LOG=$DIR/official-ws-bridge.log
cd "$DIR" || exit 1
# install under /root/forwarder for discoverability
if [ -d /root/forwarder ]; then
  cp -f "$DIR/official-ws-bridge.py" /root/forwarder/official-ws-bridge.py
  cp -f "$DIR/run-official-ws.sh" /root/forwarder/run-official-ws.sh
  chmod +x /root/forwarder/official-ws-bridge.py /root/forwarder/run-official-ws.sh
fi
while true; do
  echo "[$(date +%F\ %T)] official-ws-bridge launching" >> "$LOG"
  python3 "$DIR/official-ws-bridge.py" >> "$LOG" 2>&1
  echo "[$(date +%F\ %T)] official-ws-bridge exited, restart in 5s" >> "$LOG"
  sleep 5
done
