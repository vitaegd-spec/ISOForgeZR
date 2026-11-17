#!/usr/bin/env python3
"""
ISOForge(ZR) — a modern, lightweight editor for ISO disc images and
ZIP / 7Z / RAR archives.

Run with:
    python main.py
    python main.py --open "C:\\path\\to\\file.zip"

IMPORTANT: ISOForge always starts with a fresh, blank project.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main_window import run  # noqa: E402

if __name__ == "__main__":
    run()
