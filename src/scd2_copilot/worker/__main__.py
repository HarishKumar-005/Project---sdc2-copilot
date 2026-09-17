"""Executable module entry point for python -m src.scd2_copilot.worker."""

import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
