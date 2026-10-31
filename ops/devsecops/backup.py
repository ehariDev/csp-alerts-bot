"""Consistent encrypted backups, independent of application code and .env.

Requires a separately installed age executable and an age PUBLIC recipient. The
private recovery identity must be held off-machine. Plaintext scratch storage
must be on an encrypted, access-controlled local volume.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def snapshot(database: Path, policy: Path, approvals: Path, destination: Path) -> None:
    if not database.is_file() or destination.exists():
        raise ValueError("Existing database and a new snapshot path are required")
    source_uri = database.resolve().as_uri() + "?mode=ro"
    files = {"policy.yaml": policy.read_bytes(), "approved.json": approvals.read_bytes()}
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=destination.parent) as temporary:
        db_copy = Path(temporary) / "journal.sqlite3"
        with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(sqlite3.connect(db_copy)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
        files["journal.sqlite3"] = db_copy.read_bytes()
        # Refuse a mixed configuration snapshot if the owner edited it during backup.
        if files["policy.yaml"] != policy.read_bytes() or files["approved.json"] != approvals.read_bytes():
            raise ValueError("Configuration changed during backup")
        manifest = {
            "schema_version": 1, "created_at_utc": datetime.now(UTC).isoformat(),
            "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            "restore_rule": "Dry-run only; reconcile journal, halt and outbox before enabling sends",
        }
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))


def inspect_snapshot(path: Path, scratch: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        expected = {"journal.sqlite3", "policy.yaml", "approved.json", "manifest.json"}
        if len(archive.namelist()) != 4 or set(archive.namelist()) != expected:
            raise ValueError("Unexpected or duplicate archive members")
        if sum(info.file_size for info in archive.infolist()) > 1024 * 1024 * 1024:
            raise ValueError("Backup exceeds the pilot inspection size limit")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != 1 or set(manifest.get("sha256", {})) != expected - {"manifest.json"}:
            raise ValueError("Unsupported backup manifest")
        for name, digest in manifest["sha256"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("Backup manifest mismatch")
        with tempfile.TemporaryDirectory(prefix="inspect-", dir=scratch) as temporary:
            copied = Path(temporary) / "journal.sqlite3"
            copied.write_bytes(archive.read("journal.sqlite3"))
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup SQLite integrity check failed")
    return {"schema_version": 1, "status": "PASS", "created_at_utc": manifest["created_at_utc"],
            "live_database_modified": False, "sending_enabled": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "inspect"])
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New ciphertext backup path (create)")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--recipient", help="Public age1 recipient; never the private identity")
    parser.add_argument("--input", type=Path, help="Encrypted backup (inspect)")
    parser.add_argument("--identity-file", type=Path, help="Offline recovery identity (inspect)")
    args = parser.parse_args()
    age = shutil.which("age")
    if not age or not args.scratch.is_dir():
        parser.error("age and an existing protected scratch directory are required")
    try:
        with tempfile.TemporaryDirectory(prefix="csp-backup-", dir=args.scratch) as temporary:
            plain = Path(temporary) / "snapshot.zip"
            if args.command == "create":
                if not all((args.database, args.policy, args.approvals, args.output, args.recipient)):
                    parser.error("create requires database, policy, approvals, output, and public recipient")
                if not args.recipient.startswith("age1"):
                    parser.error("use a native age public recipient")
                snapshot(args.database, args.policy, args.approvals, plain)
                # Expose a .age recovery point only after encryption succeeds. A same-volume
                # Windows rename refuses overwrite; POSIX uses an exclusive hard link.
                with tempfile.TemporaryDirectory(prefix="ciphertext-", dir=args.output.parent) as encrypted_dir:
                    encrypted = Path(encrypted_dir) / "pending"
                    with encrypted.open("xb") as output:
                        subprocess.run([age, "-r", args.recipient, str(plain)], stdout=output,
                                       stderr=subprocess.DEVNULL, timeout=120, check=True)
                    if os.name == "nt":
                        os.rename(encrypted, args.output)
                    else:
                        os.link(encrypted, args.output)
                print(json.dumps({"schema_version": 1, "status": "PASS", "encrypted": True,
                                  "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))
            else:
                if not args.input or not args.identity_file:
                    parser.error("inspect requires input and identity-file")
                with plain.open("xb") as output:
                    subprocess.run([age, "-d", "-i", str(args.identity_file), str(args.input)], stdout=output,
                                   stderr=subprocess.DEVNULL, timeout=120, check=True)
                print(json.dumps(inspect_snapshot(plain, args.scratch.resolve())))
        return 0
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, subprocess.SubprocessError):
        print('{"schema_version":1,"status":"ERROR","reason":"backup operation failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
