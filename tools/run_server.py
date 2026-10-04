#!/usr/bin/env python3
"""启动担保风控服务端（等价于 PYTHONPATH=src python3 -m guarantee_service）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guarantee_service.__main__ import main

if __name__ == "__main__":
    main()
