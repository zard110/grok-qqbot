#!/usr/bin/env python3
"""Short-lived probe: does IDENTIFY with given intents get READY or close 4014?"""
import json, sys, time, threading, urllib.request
sys.path.insert(0, "/workspace/qq-bot-persist/official-bot")
import importlib.util
spec = importlib.util.spec_from_file_location("b", "/workspace/qq-bot-persist/official-bot/official-ws-bridge.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
import websocket
intents = int(sys.argv[1], 0)
tok = b.get_token(); gw = b.get_gateway(tok)
res = {"intents": intents}
def on_message(ws, m):
    ev = json.loads(m)
    if ev.get("op") == 10:
        ws.send(json.dumps({"op": 2, "d": {"token": f"QQBot {tok}", "intents": intents, "shard": [0, 1]}}))
    elif ev.get("t") == "READY":
        res["ready"] = True; ws.close()
    elif ev.get("op") == 9:
        res["invalid_session"] = ev.get("d"); ws.close()
    else:
        res.setdefault("other", []).append(str(ev)[:150])
def on_close(ws, code, msg):
    res["close"] = (code, msg)
ws = websocket.WebSocketApp(gw, on_message=on_message, on_close=on_close)
t = threading.Timer(10, ws.close); t.start()
ws.run_forever(ping_interval=0); t.cancel()
print(json.dumps(res, ensure_ascii=False))
