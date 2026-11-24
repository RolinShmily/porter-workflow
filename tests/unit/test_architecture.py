"""Structural guarantees the engine must keep.

These are not unit tests of behaviour — they are tests of the *architecture*, and
they exist because the corresponding rules are easy to violate by accident and
expensive to discover in production:

* a ``print()`` in the engine corrupts the MCP stdio protocol at runtime,
* an engine module importing a frontend inverts the dependency graph,
* a frontend import written as ``from porter import cli`` says ``porter`` at the
  top level, so a scan that only looks at top-level package names never sees it.

``ruff`` (rule ``T20``) and ``lint-imports`` cover most of this, but these tests
fail with a precise message and run in the normal test suite, so the rule is
enforced even if someone bypasses the linters.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import porter

PACKAGE_ROOT = Path(porter.__file__).parent
ENGINE_ROOT = PACKAGE_ROOT / "core"
ENGINE_FILES = [PACKAGE_ROOT / "__init__.py", *sorted(ENGINE_ROOT.rglob("*.py"))]

#: Directories that hold bundled frontends.
FRONTEND_DIRS = ("webui",)
#: Import prefixes for bundled frontends; ``import porter.webui`` is the engine
#: boundary, so module names are checked, not top-level package names.
FRONTEND_MODULES = tuple(f"porter.{name}" for name in FRONTEND_DIRS)


def _module_and_package(path: Path) -> tuple[str, str]:
    """Return ``(module_name, package_name)`` for a file under ``PACKAGE_ROOT``.

    ``__init__.py`` *is* its package; every other module's package is its parent
    directory. Both names start with ``porter``.
    """
    relative = path.relative_to(PACKAGE_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


def _imported_modules(tree: ast.AST, package: str) -> set[str]:
    """Every module name a module imports, with relative imports resolved.

    Collecting full names instead of top-level roots matters: ``import
    porter.cli``, ``from porter import cli`` and ``from porter.cli import app``
    all have a root of ``porter``, but only the full names reveal the frontend.
    """
    modules: set[str] = set()
    package_parts = package.split(".")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # ``from ..mcp import x`` in ``porter.cli.app`` resolves to
                # ``porter.mcp.x``: level 1 is the current package, each extra
                # level goes up one package.
                up = node.level - 1
                base = package_parts[: len(package_parts) - up] if up else package_parts
                root = base + (node.module.split(".") if node.module else [])
                modules.add(".".join(root))
                for alias in node.names:
                    if alias.name != "*":
                        modules.add(".".join([*root, alias.name]))
            elif node.module:
                modules.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        modules.add(f"{node.module}.{alias.name}")
    return modules


def _frontend_imports(imported: set[str], prefixes: tuple[str, ...]) -> list[str]:
    """The imported names that fall under any of ``prefixes``."""
    return sorted(
        module
        for module in imported
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)
    )


def test_engine_package_is_discoverable() -> None:
    """Guard against the tests silently passing because the path was wrong."""
    assert (ENGINE_ROOT / "__init__.py").is_file(), f"{ENGINE_ROOT} is not a package"
    assert len(ENGINE_FILES) > 1, f"no engine modules found under {ENGINE_ROOT}"


@pytest.mark.parametrize(
    "path", ENGINE_FILES, ids=lambda p: str(p.relative_to(PACKAGE_ROOT))
)
def test_engine_never_prints(path: Path) -> None:
    """The engine must never write to stdout.

    In an MCP stdio server stdout carries the JSON-RPC protocol. Use
    ``porter.core.logging.get_logger`` instead.
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
        f"{path.relative_to(PACKAGE_ROOT)} calls print() on line(s) {offenders}. "
        "Engine code must log to stderr, not stdout."
    )


@pytest.mark.parametrize(
    "path", ENGINE_FILES, ids=lambda p: str(p.relative_to(PACKAGE_ROOT))
)
def test_engine_does_not_import_frontends(path: Path) -> None:
    """The dependency arrow points one way: frontends -> engine."""
    _, package = _module_and_package(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = _frontend_imports(_imported_modules(tree, package), FRONTEND_MODULES)
    assert not forbidden, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {forbidden}. "
        "The engine must not depend on a frontend."
    )


def test_version_is_a_string() -> None:
    assert isinstance(porter.__version__, str)
    assert porter.__version__.count(".") >= 1


@pytest.mark.parametrize("name", sorted(porter._LAZY_EXPORTS))
def test_public_api_is_lazily_exported(name: str) -> None:
    """Every advertised symbol resolves from the engine and is then cached.

    Iterating the map instead of spot-checking two names means a typo in any
    entry fails here rather than in a user's first ``from porter import ...``.
    """
    module_path = porter._LAZY_EXPORTS[name]
    assert module_path.startswith("porter.core."), (
        f"{name} is resolved from {module_path}; the public API must point into "
        "the engine, not a frontend"
    )
    value = getattr(porter, name)
    assert value is getattr(importlib.import_module(module_path), name)
    # Cached into globals() after the first lookup.
    assert name in vars(porter)


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        _ = porter.this_symbol_does_not_exist


def test_every_lazy_export_is_listed_in_all() -> None:
    missing = sorted(set(porter._LAZY_EXPORTS) - set(porter.__all__))
    assert not missing, f"_LAZY_EXPORTS entries missing from __all__: {missing}"


def test_workflow_api_is_exported_from_root() -> None:
    for name in (
        "WorkflowPlan",
        "WorkflowResult",
        "PlanOverrides",
        "ExecutionOutcome",
        "resolve_plan",
        "execute_plan",
        "probe_source",
        "capability_catalog",
    ):
        assert name in porter.__all__
        assert hasattr(porter, name)


@pytest.mark.parametrize("frontend", FRONTEND_DIRS)
def test_frontend_packages_are_discoverable(frontend: str) -> None:
    """Guard against an empty ``rglob`` making the sibling test vacuous."""
    files = sorted((PACKAGE_ROOT / frontend).rglob("*.py"))
    assert files, f"no modules found under {PACKAGE_ROOT / frontend}"


@pytest.mark.parametrize("frontend", FRONTEND_DIRS)
def test_frontends_do_not_import_each_other(frontend: str) -> None:
    """``porter.cli``, ``porter.mcp``, and ``porter.webui`` are siblings, not layers.

    All three depend on the engine; none may depend on another. The
    ``independence`` import-linter contract is the authoritative gate; this check
    remains because it runs in the ordinary suite and names the offending file.
    """
    root = PACKAGE_ROOT / frontend
    others = tuple(f"porter.{name}" for name in FRONTEND_DIRS if name != frontend)
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        _, package = _module_and_package(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _frontend_imports(_imported_modules(tree, package), others)
        if imported:
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported}")

    assert not offenders, (
        f"porter.{frontend} must not import {list(others)}: " + "; ".join(offenders)
    )


# ----------------------------------------------------------------------
# Text encoding
# ----------------------------------------------------------------------


def _constant_modes(node: ast.expr) -> list[str] | None:
    """Every string an expression can evaluate to, or ``None`` if not constant.

    A ternary mode (``"ab" if resuming else "wb"``) is an ``IfExp`` rather than a
    literal, and treating an unrecognised expression as "unknown, therefore
    text" is exactly how a binary write gets reported as an unencoded one.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.IfExp):
        body, other = _constant_modes(node.body), _constant_modes(node.orelse)
        if body is None or other is None:
            return None
        return body + other
    return None


def _is_binary_open(node: ast.Call) -> bool:
    """Whether an ``open`` call uses a binary mode, where no encoding applies.

    The mode is the *first* positional argument here, not the second: only
    attribute calls are inspected, so this is always ``Path.open(mode)``. The
    builtin ``open(file, mode)`` is a ``Name`` call and is never matched, which is
    also why it needs no handling.
    """
    modes: list[str] | None = None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            modes = _constant_modes(keyword.value)
    if modes is None and node.args:
        modes = _constant_modes(node.args[0])
    # Every mode it could take has to be binary: one branch we cannot read means
    # we cannot make the claim, and the safer answer is to keep looking.
    return modes is not None and all("b" in mode for mode in modes)


def _text_writes_without_encoding(tree: ast.AST) -> list[int]:
    """Lines where text is written without naming an encoding."""
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        if name not in {"write_text", "open"}:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        if name == "open" and _is_binary_open(node):
            continue
        offenders.append(node.lineno)
    return offenders


@pytest.mark.parametrize(
    "path",
    sorted(PACKAGE_ROOT.rglob("*.py")),
    ids=lambda p: str(p.relative_to(PACKAGE_ROOT)),
)
def test_text_writes_name_their_encoding(path: Path) -> None:
    """No text write may fall back to the *locale* encoding.

    ``Path.write_text`` and text-mode ``open`` default to the locale encoding,
    which is not UTF-8 on Windows (GBK on the development box). Every subtitle,
    transcript and metadata file porter writes can hold CJK or an emoji, so the
    locale default is a crash waiting for the first such character: the write
    raises ``UnicodeEncodeError`` and kills a job whose work was already done.

    This is checked mechanically because a behavioural test cannot catch it on
    CI: Ubuntu's locale is UTF-8, so ``write_text`` without an encoding produces
    exactly the same bytes there, and the bug only ever appears on a user's
    Windows machine. Two such writes shipped in ``platforms/base.py`` and were
    found by reading, not by the suite.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = _text_writes_without_encoding(tree)

    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)}: text written without an encoding at line(s) "
        f"{offenders}; pass encoding='utf-8'"
    )
