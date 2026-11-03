#!/usr/bin/env python3
"""CLI for the ppt_poster plugin (SenseNova posters / slide decks); logic lives in core/sensenova.py.

  sensenova_make.py poster --prompt "..." [--ref img.png] [--out DIR|x.png] [--no-image] [--name 标题] [--no-repair]
  sensenova_make.py ppt --topic "..." [--slides 6] [--images 2] [--extra "..."] [--name 标题] [--out DIR] [--no-repair]
  sensenova_make.py rerender DIR [--name 标题]      (no API calls)

stdout: one JSON object; exit 0 ok, 2 failure, 3 quota/rate limit, 4 auth/key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.sensenova import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
