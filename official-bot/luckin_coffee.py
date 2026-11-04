#!/usr/bin/env python3
"""Worker / admin CLI for the luckin_coffee plugin (瑞幸自提点单); logic lives in core/luckin.py.

  luckin_coffee.py check                      verify the token against the Luckin MCP (prints tool names only)
  luckin_coffee.py enable --group zwift       turn the plugin on for one group (features + enabled_plugins)
  luckin_coffee.py disable --group zwift
  luckin_coffee.py step --group G --event '{"member":..,"text":..,"msg_id":..,"at_me":true}'   (spawned by the gateway)
  luckin_coffee.py poll --group G --member M --order ORDER_ID                                  (spawned after createOrder)

The token is read from LUCKIN_MCP_TOKEN or official-bot/luckin-token (chmod 600) and never printed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.luckin import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
