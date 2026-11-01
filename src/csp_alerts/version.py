"""Code identity (WD-002): the Git revision when the project sits in a repository, plus a source content manifest.

Read-only and offline. The Git revision is read straight from the repository metadata files (HEAD, loose refs,
packed-refs) so no subprocess is needed. A repository is only *detected*; this module never creates, initialises
or modifies one. The manifest hash covers the application packages' own source (`*.py`, `*.sql`) and never
includes `.env`, databases, logs, account records or approvals, so it is safe to store in run records.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent            # .../csp_alerts
_SRC_DIR = _PKG_DIR.parent                            # .../src (or site-packages)
_PACKAGES = ("csp_alerts", "csp_dashboard")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CodeIdentity:
    git_sha: str | None          # None when no enclosing repository (or it has no commits yet)
    manifest_sha256: str         # hash over every (relative path, file sha256) pair
    file_count: int

    @property
    def label(self) -> str:
        """Short owner-facing identity: the Git commit when known, else the manifest hash prefix."""
        return f"git:{self.git_sha[:12]}" if self.git_sha else f"manifest:{self.manifest_sha256[:12]}"


def find_git_dir(start: Path | None = None) -> Path | None:
    """Nearest enclosing `.git` directory (or the target of a `.git` file), or None."""
    here = (start or _SRC_DIR).resolve()
    for d in (here, *here.parents):
        g = d / ".git"
        try:
            if g.is_dir():
                return g
            if g.is_file():
                txt = g.read_text(encoding="utf-8").strip()
                if txt.startswith("gitdir:"):
                    target = (d / txt[len("gitdir:"):].strip()).resolve()
                    return target if target.is_dir() else None
        except OSError:
            return None
    return None


def git_revision(git_dir: Path | None = None) -> str | None:
    """Full commit SHA of HEAD read from repository files; None if unavailable. Does not detect a dirty tree."""
    g = git_dir if git_dir is not None else find_git_dir()
    if g is None:
        return None
    try:
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if _SHA.match(head):
            return head
        if not head.startswith("ref:"):
            return None
        ref = head[4:].strip()
        loose = g / ref
        if loose.is_file():
            val = loose.read_text(encoding="utf-8").strip()
            return val if _SHA.match(val) else None
        packed = g / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref and _SHA.match(parts[0]):
                    return parts[0]
    except OSError:
        return None
    return None


def source_manifest(src_dir: Path | None = None) -> dict[str, str]:
    """{relative posix path: sha256} for the application packages' source files."""
    base = (src_dir or _SRC_DIR).resolve()
    out: dict[str, str] = {}
    for pkg in _PACKAGES:
        root = base / pkg
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix not in (".py", ".sql") or "__pycache__" in p.parts or not p.is_file():
                continue
            out[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def manifest_hash(manifest: dict[str, str]) -> str:
    blob = "\n".join(f"{k}  {manifest[k]}" for k in sorted(manifest))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def code_identity() -> CodeIdentity:
    m = source_manifest()
    return CodeIdentity(git_revision(), manifest_hash(m), len(m))
