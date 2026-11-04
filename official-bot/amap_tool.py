#!/usr/bin/env python3
"""Worker / admin CLI for the amap plugin (高德：附近找店 / 骑行路线 / 骑行天气 / 中点聚餐); logic in core/amap.py.

  amap_tool.py check                          one geocode call to verify the key (never prints it)
  amap_tool.py usage                          this month's local API counters per quota bucket
  amap_tool.py dry-run "南京新街口附近的瑞幸" [--out /tmp/x]   build a reply without sending
  amap_tool.py enable --group zwift           turn the plugin on for one group (features + enabled_plugins)
  amap_tool.py disable --group zwift
  amap_tool.py query --group G --event '{"member":..,"text":..,"msg_id":..,"at_me":true}'   (spawned by the gateway)

The Web-service key is read from AMAP_WEB_KEY / AMAP_KEY or official-bot/amap-key (chmod 600) and never printed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.amap import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
