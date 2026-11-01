"""P0-01: dependency / no-broker boundary (static import audit)."""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from csp_alerts import boundary

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "csp_alerts"


def audit_tree(root: Path, exempt: set[str] | None = None) -> list[str]:
    """Return violations: forbidden imports, dynamic imports, order-like callables."""
    exempt = exempt or set()
    problems: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if boundary.is_forbidden_module(a.name):
                        problems.append(f"{rel}:{node.lineno} import {a.name}")
                    if a.name.split(".")[0] == "importlib":
                        problems.append(f"{rel}:{node.lineno} dynamic import machinery importlib")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] == "importlib":
                    problems.append(f"{rel}:{node.lineno} dynamic import machinery importlib")
                if boundary.is_forbidden_module(mod):
                    problems.append(f"{rel}:{node.lineno} from {mod}")
                for a in node.names:
                    if boundary.is_forbidden_module(f"{mod}.{a.name}"):
                        problems.append(f"{rel}:{node.lineno} from {mod} import {a.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in boundary.FORBIDDEN_CALLABLE_NAMES:
                    problems.append(f"{rel}:{node.lineno} defines {node.name}")
            elif isinstance(node, ast.Attribute):
                if node.attr in boundary.FORBIDDEN_CALLABLE_NAMES:
                    problems.append(f"{rel}:{node.lineno} attribute {node.attr}")
            elif isinstance(node, ast.Name):
                if node.id in {"__import__", "importlib", "exec", "eval"}:
                    problems.append(f"{rel}:{node.lineno} dynamic import/exec name {node.id}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if boundary.is_forbidden_module(node.value.strip()):
                    problems.append(f"{rel}:{node.lineno} string literal names forbidden module")
    return problems


def test_src_has_no_forbidden_imports():
    # boundary.py legitimately lists forbidden names as string constants.
    assert audit_tree(SRC, exempt={"boundary.py"}) == []


def test_auditor_detects_planted_violations(tmp_path):
    (tmp_path / "bad.py").write_text(
        "import schwab.auth\n"
        "from options_scanner.trade_actions import x\n"
        "from options_scanner import confirm_gate\n"
        "import importlib\n"
        "def place_order(): pass\n"
        "client.submit_order()\n"
    )
    text = "\n".join(audit_tree(tmp_path))
    assert "import schwab.auth" in text
    assert "trade_actions" in text
    assert "confirm_gate" in text
    assert "importlib" in text
    assert "defines place_order" in text
    assert "attribute submit_order" in text


def test_pyproject_has_no_broker_dependencies():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = list(data["project"]["dependencies"])
    for group in data.get("dependency-groups", {}).values():
        names += [g for g in group if isinstance(g, str)]
    norm = {re.split(r"[<>=!~\[ ;]", n, maxsplit=1)[0].lower().replace("_", "-") for n in names}
    assert norm.isdisjoint(set(boundary.FORBIDDEN_DISTRIBUTIONS)), norm
    assert data["project"]["requires-python"].startswith(">=3.12")


def test_runtime_modules_clean_after_importing_package():
    import csp_alerts.cli  # noqa: F401
    import csp_alerts.journal.store  # noqa: F401
    import csp_alerts.policy.entry  # noqa: F401

    assert boundary.loaded_forbidden_modules() == []
    boundary.assert_no_broker_modules_loaded()


def test_no_float_in_money_modules():
    """Decimal everywhere: no float annotations or float() calls in policy/journal/market."""
    offenders = []
    for sub in ("policy", "journal", "market"):
        for path in (SRC / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "float":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_cli_has_no_trade_commands():
    from csp_alerts import cli

    text = cli.build_parser().format_help().lower()
    for word in ("place", "order", "execute", "broker", "roll", "live-trade"):
        assert word not in text
