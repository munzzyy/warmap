"""The executable's entry point. PyInstaller freezes this file; it only hands
off to the real command line."""

import sys

from warmap.cli import main

if __name__ == "__main__":
    sys.exit(main())
