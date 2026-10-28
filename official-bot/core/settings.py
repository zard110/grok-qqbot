"""One configurable data root for the gateway, CLI and administration API."""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("QQBOT_DATA_DIR", str(CODE_DIR))).expanduser().resolve()
