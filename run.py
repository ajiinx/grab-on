"""Convenience launcher: `python run.py` works from any directory."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.__main__ import main  # noqa: E402

main()
