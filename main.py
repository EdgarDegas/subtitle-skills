from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC = PROJECT_ROOT / "src"
# Load this Skill's code even if an obsolete package is installed elsewhere.
sys.dont_write_bytecode = True
sys.path.insert(0, str(SRC))

from codex_subtitles.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
