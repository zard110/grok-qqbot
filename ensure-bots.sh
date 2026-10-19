#!/bin/bash
if [ ! -x /root/Napcat/opt/QQ/qq ] && [ -f /workspace/qq-bot-persist/Napcat.tar.gz ]; then
  tar xzf /workspace/qq-bot-persist/Napcat.tar.gz -C /root
fi
# restore forwarder scripts from persist if missing
if [ ! -d /root/forwarder ] && [ -d /workspace/qq-bot-persist/forwarder ]; then
  cp -a /workspace/qq-bot-persist/forwarder /root/forwarder
fi
# restore webhook url/key from persist (lost on machine swap)
if [ -d /root/forwarder ]; then
  if [ ! -f /root/forwarder/url ] && [ -f /workspace/qq-bot-persist/forwarder-url ]; then
    cp /workspace/qq-bot-persist/forwarder-url /root/forwarder/url
    chmod 600 /root/forwarder/url
  fi
  if [ ! -f /root/forwarder/key ] && [ -f /workspace/qq-bot-persist/forwarder-key ]; then
    cp /workspace/qq-bot-persist/forwarder-key /root/forwarder/key
    chmod 600 /root/forwarder/key
  fi
  # keep official bridge scripts in sync under /root/forwarder
  if [ -f /workspace/qq-bot-persist/official-bot/official-ws-bridge.py ]; then
    cp -f /workspace/qq-bot-persist/official-bot/official-ws-bridge.py /root/forwarder/official-ws-bridge.py
    chmod +x /root/forwarder/official-ws-bridge.py
  fi
  if [ -f /workspace/qq-bot-persist/official-bot/run-official-ws.sh ]; then
    cp -f /workspace/qq-bot-persist/official-bot/run-official-ws.sh /root/forwarder/run-official-ws.sh
    chmod +x /root/forwarder/run-official-ws.sh
  fi
fi
for f in ensure-bots.sh napcat-run.sh qq-healer.sh qr-watch.sh; do
  if [ ! -f /root/$f ] && [ -f /workspace/qq-bot-persist/$f ]; then
    cp /workspace/qq-bot-persist/$f /root/$f
    chmod +x /root/$f
  fi
done
# always refresh ensure-bots from persist when present
if [ -f /workspace/qq-bot-persist/ensure-bots.sh ]; then
  cp -f /workspace/qq-bot-persist/ensure-bots.sh /root/ensure-bots.sh
  chmod +x /root/ensure-bots.sh
fi
# (xvfb only needed for NapCat; retired)
# NapCat retired 2026-10-19 (only official MaxShenBi kept): pgrep -f /root/napcat-run.sh >/dev/null || setsid nohup /root/napcat-run.sh >/dev/null 2>&1 < /dev/null &
# NapCat retired 2026-10-19 (only official MaxShenBi kept): pgrep -f /root/qq-healer.sh >/dev/null || setsid nohup /root/qq-healer.sh >/dev/null 2>&1 < /dev/null &
if [ -d /root/forwarder ]; then
# NapCat retired 2026-10-19 (only official MaxShenBi kept): pgrep -f /root/forwarder/run.sh >/dev/null || setsid nohup /root/forwarder/run.sh >/dev/null 2>&1 < /dev/null &
  # NapCat ws-bridge: KEEP OFF while official MaxShenBi bridge is primary (avoids double replies).
  # To re-enable NapCat forward: start bash /root/forwarder/run-ws.sh and stop official-ws.
  # pgrep -f /root/forwarder/ws-bridge.py >/dev/null || setsid nohup bash /root/forwarder/run-ws.sh >/dev/null 2>&1 < /dev/null &
fi
# Official QQ bot gateway bridge (does NOT require NapCat)
if [ -f /workspace/qq-bot-persist/official-bot/run-official-ws.sh ]; then
  pgrep -f official-ws-bridge.py >/dev/null || setsid nohup bash /workspace/qq-bot-persist/official-bot/run-official-ws.sh >/dev/null 2>&1 < /dev/null &
fi
exit 0
