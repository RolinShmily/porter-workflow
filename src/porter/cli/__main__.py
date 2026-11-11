"""Module entry point: ``python -m porter.cli`` and the ``porter`` console script.

The console script is declared as ``porter.cli.__main__:main`` in
``pyproject.toml``, so this module must expose ``main``.
"""

from __future__ import annotations

import sys

from porter.cli.app import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
