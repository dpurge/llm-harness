"""Front-end-only frameworks are imported only under `frontends/`.

The check parses source with `ast`; no module is executed. A mention in a
docstring or comment is not an import and does not count.
"""

import ast
from pathlib import Path

FRONTEND_ONLY = frozenset({"nicegui", "textual", "starlette"})
SRC = Path(__file__).resolve().parent.parent / "src" / "llm_harness"


def frontend_imports(source: str) -> set[str]:
    """Top-level package names of every front-end-only import, at any nesting."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots = {node.module.split(".")[0]}
        else:
            continue
        found |= roots & FRONTEND_ONLY
    return found


def test_the_scanner_detects_front_end_imports() -> None:
    assert frontend_imports("import nicegui") == {"nicegui"}
    assert frontend_imports("from textual.app import App") == {"textual"}
    assert frontend_imports("def f():\n    import starlette.routing\n") == {"starlette"}
    assert frontend_imports('"""import nicegui"""\nimport json\n') == set()


def test_only_frontends_import_front_end_frameworks() -> None:
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if relative.parts[0] == "frontends":
            continue
        found = frontend_imports(path.read_text(encoding="utf-8"))
        if found:
            offenders[relative.as_posix()] = sorted(found)
    assert offenders == {}
