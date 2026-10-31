"""Build an integrity-manifested candidate. No publishing, installation, or live application access."""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from readiness import ROOT, UPSTREAM_SHA, git, publication_check, verify_upstream


def wheel_check(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if "csp_alerts/cli.py" not in names:
            raise ValueError("CLI is missing from the wheel")
        if not any(n.startswith("csp_alerts/journal/migrations/") and n.endswith(".sql") for n in names):
            raise ValueError("SQLite migrations are missing from the wheel")
        if any(".." in Path(n).parts or Path(n).is_absolute() for n in names):
            raise ValueError("Unsafe wheel member")


def build(root: Path = ROOT) -> Path:
    verify_upstream(root)
    result = publication_check(root)
    if result["status"] != "PASS":
        raise ValueError("Publication privacy check failed")
    commit = git(root, "rev-parse", "HEAD")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release requires a clean committed source tree")
    work = root / "ops/devsecops/_work"
    wheels = list((work / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Exactly one wheel is required")
    wheel_check(wheels[0])
    payload = {
        f"wheel/{wheels[0].name}": wheels[0].read_bytes(),
        "runtime.txt": (work / "runtime.txt").read_bytes(),
        "runtime.cdx.json": (work / "runtime.cdx.json").read_bytes(),
    }
    # Archive the pinned Git object, never local private/untracked upstream files.
    upstream = subprocess.run(
        ["git", "-C", str(root / "third_party/stockpile"), "archive", "--format=tar", UPSTREAM_SHA],
        capture_output=True, check=True, timeout=60,
    ).stdout
    payload["stockpile.tar"] = upstream
    for name, digest in result["files"].items():
        data = (root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Source changed while packaging")
        payload[f"source/{name}"] = data
    manifest = {
        "schema_version": 1, "application_commit": commit, "stockpile_commit": UPSTREAM_SHA,
        "purpose": "paper-pilot candidate; not installed or approved for live sends",
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())},
    }
    payload["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    output = work / "release"
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"csp-alerts-{commit[:12]}.zip"
    # Exclusive creation prevents accidentally replacing a reviewed artifact.
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    return target


if __name__ == "__main__":
    print(build())
