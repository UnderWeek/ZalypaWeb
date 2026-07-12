from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Widget tests do not need a physical display.  WebEngine is exercised by the
# explicit application smoke command documented in README.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
