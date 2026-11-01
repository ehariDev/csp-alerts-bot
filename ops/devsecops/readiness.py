"""Independent release checks. Never imports the application or loads its .env.

Only explicit publication candidates are read. Output contains paths and hashes,
never file contents or secret values. Git checks use no credentials or network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_SHA = "34bf6cf8101e54620ea1e2bda940f0a3aa88e791"
TOP_FILES = {
    "README.md", "RUNBOOK.md", "SECURITY.md", "DATA_DICTIONARY.md",
    "pyproject.toml", "uv.lock", ".gitignore", ".env.example",
}
TREES = {"src", "tests", "docs", "requirements", "ops", "config", ".github"}
SKIP_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "_work", "data", "third_party", "node_modules", "dist", "build",
}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".p12", ".age"}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        raise ValueError("Git check failed; repository or revision is unavailable")
    return result.stdout.decode("utf-8").strip()


def private_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".env") and name != ".env.example"
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or name.endswith(("-wal", "-shm", ".pyc"))
        or any(p.lower() in {"data", ".venv", "third_party", "__pycache__"} for p in path.parts)
        or path.as_posix().lower() == "config/config.yaml"
    )


def candidates(root: Path) -> list[Path]:
    found = []
    for p in sorted(root.iterdir()):
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            if p.name in TOP_FILES | TREES:
                raise ValueError("Publication root contains a symbolic link")
            continue
        if p.name in TOP_FILES and p.is_file():
            found.append(p)
        elif p.name in TREES and p.is_dir():
            # Do not follow symlinks or junctions into private/external directories.
            pending = [p]
            while pending:
                folder = pending.pop()
                for child in sorted(folder.iterdir()):
                    if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                        raise ValueError("Publication tree contains a symbolic link or junction")
                    if child.name in SKIP_PARTS or child.name.endswith(".egg-info"):
                        continue
                    if child.is_dir():
                        pending.append(child)
                    elif child.is_file() and not private_path(child.relative_to(root)):
                        found.append(child)
    return sorted(found)


def publication_check(root: Path) -> dict:
    problems = []
    # Enforce privacy for all tracked paths, including paths outside the allowlist.
    if (root / ".git").exists():
        tracked = git(root, "ls-files", "-z").split("\0")
        problems += [f"private path is tracked: {p}" for p in tracked if p and private_path(Path(p))]
    approval = root / "config/approved.json"
    if approval.exists():
        value = json.loads(approval.read_text(encoding="utf-8-sig"))
        # Ship an empty owner whitelist, regardless of the loader's accepted envelope.
        if value not in ([], {"approved_underlyings": []}):
            problems.append("config/approved.json is not an empty publication whitelist")
    files = candidates(root)
    return {"schema_version": 1, "status": "FAIL" if problems else "PASS", "problems": problems,
            "files": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


def verify_upstream(root: Path) -> dict:
    upstream = root / "third_party/stockpile"
    if git(upstream, "rev-parse", "HEAD") != UPSTREAM_SHA:
        raise ValueError("Stockpile HEAD differs from the approved revision")
    # Ignore caches, but reject tracked modifications and any untracked Python code.
    if git(upstream, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Stockpile tracked files are modified")
    untracked = git(upstream, "ls-files", "--others", "--exclude-standard").splitlines()
    if any(p.endswith((".py", ".pth", ".so", ".pyd")) for p in untracked):
        raise ValueError("Stockpile contains untracked executable code")
    # Also catch ignored source additions; imports can load them even when Git ignores them.
    tracked = set(git(upstream, "ls-files").splitlines())
    for sub in ("options-scanner/options_scanner", "shared/stocks_shared"):
        for p in (upstream / sub).rglob("*"):
            if p.is_symlink():
                raise ValueError("Stockpile runtime tree contains a symbolic link")
            if p.is_file() and p.suffix in {".py", ".pth", ".pyd", ".so"}:
                if p.relative_to(upstream).as_posix() not in tracked:
                    raise ValueError("Stockpile runtime tree contains unreviewed executable code")
    return {"schema_version": 1, "status": "PASS", "upstream_sha": UPSTREAM_SHA}


def check_workflows(root: Path) -> dict:
    problems = []
    for p in (root / ".github/workflows").glob("*.yml"):
        text = p.read_text(encoding="utf-8")
        if "pull_request_target" in text or "runs-on: self-hosted" in text:
            problems.append(f"unsafe trigger or runner: {p.name}")
        for action in re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)", text):
            if not action.startswith("./") and not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action):
                problems.append(f"action is not SHA-pinned: {p.name}")
    return {"schema_version": 1, "status": "FAIL" if problems else "PASS", "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publication-check", "verify-upstream", "check-workflows"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = {"publication-check": publication_check, "verify-upstream": verify_upstream,
                  "check-workflows": check_workflows}[args.command](args.root.resolve())
        content = json.dumps(result, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return 0 if result["status"] == "PASS" else 1
    except (OSError, ValueError, subprocess.SubprocessError):
        # No raw exception text: paths, command output, and provider responses may be private.
        print('{"schema_version":1,"status":"ERROR","reason":"check could not complete"}')
        return 2


if __name__ == "__main__":
    sys.exit(main())
