"""Create a sanitized publication snapshot; never initialize Git in the source workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from readiness import ROOT, publication_check

EXTRA_IGNORES = """
# Publication safety additions (only this staged copy is changed).
.env.*
!.env.example
*.pem
*.key
*.pfx
*.p12
*.age
dist/
build/
.ruff_cache/
"""


def stage(root: Path, destination: Path) -> dict:
    result = publication_check(root)
    if result["status"] != "PASS":
        raise ValueError("Publication checks failed")
    if destination.exists():
        raise ValueError("Destination must not exist; existing files are never overwritten")
    destination.mkdir(parents=True)
    for name, expected in result["files"].items():
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source changed or escaped the publication root")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Source changed during staging; create a new snapshot")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
    ignore = destination / ".gitignore"
    with ignore.open("a", encoding="utf-8") as handle:
        handle.write(EXTRA_IGNORES)
    manifest = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in destination.rglob("*") if p.is_file()}
    (destination / "PUBLICATION_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"status": "PASS", "file_count": len(manifest)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(stage(args.root.resolve(), args.destination.resolve())))
