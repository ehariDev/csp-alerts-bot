"""python -m csp_dashboard {start | hash-password | version-info}. Starting does not scan, send, migrate or write."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from csp_alerts.settings import StartupError, load_settings

from csp_dashboard import auth


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csp-dashboard", description="Local read-only dashboard (127.0.0.1 only).")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--policy", default="config/policy.yaml")
    p.add_argument("--approved", default="config/approved.json")
    sub = p.add_subparsers(dest="command", required=True)
    st = sub.add_parser("start", help="serve the five read-only views on 127.0.0.1 (Ctrl+C to stop)")
    st.add_argument("--port", type=int, default=None, help="default: CSP_DASHBOARD_PORT or 8765")
    sub.add_parser("hash-password", help="prompt for a password and print the .env line to paste (never stores anything)")
    vi = sub.add_parser("version-info", help="print code identity (Git revision if any, source manifest hash)")
    vi.add_argument("--write-manifest", action="store_true", help="save the manifest under data/manifests/")
    return p


def cmd_hash(_args) -> int:
    pw = getpass.getpass("New dashboard password (min 12 chars, not echoed): ")
    if pw != getpass.getpass("Repeat: "):
        print("FAIL passwords differ", file=sys.stderr)
        return 2
    try:
        line = auth.make_hash(pw)
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    print("Paste this line into .env (replace the existing CSP_DASHBOARD_PASSWORD_HASH line; never commit .env):")
    print(f"CSP_DASHBOARD_PASSWORD_HASH={line}")
    return 0


def cmd_version(args) -> int:
    from csp_alerts.version import code_identity, find_git_dir, source_manifest
    ident = code_identity()
    print(json.dumps({"label": ident.label, "git_sha": ident.git_sha, "git_repository_found": find_git_dir() is not None,
                      "manifest_sha256": ident.manifest_sha256, "files": ident.file_count}, indent=2))
    if args.write_manifest:
        out = Path("data") / "manifests" / f"manifest-{ident.manifest_sha256[:12]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"git_sha": ident.git_sha, "manifest_sha256": ident.manifest_sha256, "files": source_manifest()},
                                  indent=1, sort_keys=True), encoding="utf-8")
        print(f"manifest written to {out}")
    return 0


def cmd_start(args) -> int:
    from csp_dashboard import views
    from csp_dashboard.server import DashboardServer, serve
    s = load_settings(args.env_file)
    hashed = s.dashboard_password_hash.reveal()
    if not hashed:
        print("FAIL CSP_DASHBOARD_PASSWORD_HASH is not set in .env. Run: python -m csp_dashboard hash-password", file=sys.stderr)
        return 2
    sources = views.Sources(Path(s.database_path), Path(args.policy), Path(args.approved), s.tz)
    srv = DashboardServer(sources, hashed, args.port if args.port is not None else s.dashboard_port)
    port = srv.server_address[1]
    print(f"Dashboard (read-only) listening on http://127.0.0.1:{port}/  - Ctrl+C to stop. Starts no scans, sends nothing.", flush=True)
    if not Path(s.database_path).is_file():
        print("note: the journal file does not exist yet; views will say so (run `csp journal init`).", flush=True)
    serve(srv)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"start": cmd_start, "hash-password": cmd_hash, "version-info": cmd_version}[args.command](args)
    except StartupError as exc:
        print("FAIL startup validation:", file=sys.stderr)
        for pr in exc.problems:
            print(f"  - {pr}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"FAIL cannot start: {type(exc).__name__}: {exc.strerror or 'unknown'} (is the port already in use?)", file=sys.stderr)
        return 2
