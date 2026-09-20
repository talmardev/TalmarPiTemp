#!/usr/bin/env python3
"""TalmarPiTemp entry point. Run: python3 monitor.py"""

import sys

if sys.version_info < (3, 9):
    sys.exit("TalmarPiTemp needs Python 3.9 or newer.")

from talmarpitemp.cli import main

if __name__ == "__main__":
    sys.exit(main())
