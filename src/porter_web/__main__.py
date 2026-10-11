"""Module entry point for ``python -m porter_web``."""

import sys

from porter_web.app import main

if __name__ == "__main__":
    sys.exit(main())
