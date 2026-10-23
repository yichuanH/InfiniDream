import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "core", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
