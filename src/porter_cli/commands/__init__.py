"""Command implementations for the ``porter`` CLI.

Each module exposes two functions:

``configure(subparsers)``
    Register its subcommand and attach ``handler=<module>.run``.
``run(args) -> int``
    Execute the command and return a process exit code.

Handlers only render; all real work is delegated to the ``porter`` engine
so the MCP frontend can reuse it.
"""

__all__: list[str] = []
