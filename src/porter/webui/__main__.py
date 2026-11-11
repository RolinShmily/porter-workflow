"""Module entry point for ``python -m porter.webui``."""

import sys

from porter.webui.app import main

if __name__ == "__main__":
    sys.exit(main())
