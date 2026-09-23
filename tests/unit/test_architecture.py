"""Structural guarantees the engine must keep.

These are not unit tests of behaviour — they are tests of the *architecture*, and
they exist because the corresponding rules are easy to violate by accident and
expensive to discover in production:

* a ``print()`` in the engine corrupts the MCP stdio protocol at runtime,
* an engine module importing a frontend inverts the dependency graph.

``ruff`` (rule ``T20``) and ``lint-imports`` cover most of this, but these tests
fail with a precise message and run in the normal test suite, so the rule is
enforced even if someone bypasses the linters.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import porter

ENGINE_ROOT = Path(porter.__file__).parent
ENGINE_FILES = sorted(ENGINE_ROOT.rglob("*.py"))

SRC_ROOT = ENGINE_ROOT.parent
FRONTEND_PACKAGES = {"porter_cli", "porter_mcp"}


def _imported_roots(tree: ast.AST) -> set[str]:
    """Top-level package names imported by a module."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, stays inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_engine_package_is_discoverable() -> None:
    """Guard against the tests silently passing because the path was wrong."""
    assert ENGINE_FILES, f"no engine modules found under {ENGINE_ROOT}"


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_engine_never_prints(path: Path) -> None:
    """The engine must never write to stdout.

    In an MCP stdio server stdout carries the JSON-RPC protocol. Use
    ``porter.logging.get_logger`` instead.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert not offenders, (
        f"{path.relative_to(ENGINE_ROOT)} calls print() on line(s) {offenders}. "
        "Engine code must log to stderr, not stdout."
    )


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_engine_does_not_import_frontends(path: Path) -> None:
    """The dependency arrow points one way: frontends -> engine."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = _imported_roots(tree) & FRONTEND_PACKAGES
    assert not forbidden, (
        f"{path.relative_to(ENGINE_ROOT)} imports {sorted(forbidden)}. "
        "The engine must not depend on a frontend."
    )


def test_version_is_a_string() -> None:
    assert isinstance(porter.__version__, str)
    assert porter.__version__.count(".") >= 1


def test_public_api_is_lazily_exported() -> None:
    """``import porter`` stays cheap; heavier symbols resolve on first access."""
    assert porter.JobOptions is not None
    assert porter.Pipeline is not None
    # Cached into globals() after the first lookup.
    assert "JobOptions" in vars(porter)


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        _ = porter.this_symbol_does_not_exist


@pytest.mark.parametrize("frontend", sorted(FRONTEND_PACKAGES))
def test_frontends_do_not_import_each_other(frontend: str) -> None:
    """``porter_cli`` and ``porter_mcp`` are siblings, not layers.

    Both depend on ``porter``; neither may depend on the other. This is checked
    here rather than by an import-linter contract because import-linter can only
    resolve contract *sources* that live inside ``root_package``.
    """
    root = SRC_ROOT / frontend
    others = FRONTEND_PACKAGES - {frontend}
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_roots(tree) & others
        if imported:
            offenders.append(f"{path.relative_to(SRC_ROOT)} -> {sorted(imported)}")

    assert not offenders, (
        f"{frontend} must not import {sorted(others)}: " + "; ".join(offenders)
    )
