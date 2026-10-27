#!/bin/bash
DIR=/workspace/qq-bot-persist/official-bot
cd "$DIR" || exit 1
while true; do
  python3 "$DIR/local-watchdog.py" >> "$DIR/local-watchdog.log" 2>&1
  sleep 5
done
