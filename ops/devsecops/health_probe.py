"""Optional five-minute external health ping. Never forwards application output.

Use a protected file containing a Healthchecks.io hc-ping.com URL, configured to
notify the owner through an independent channel after a 15-minute grace period.
No endpoint is provisioned or contacted until this command is explicitly run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(text: str) -> str:
    value = text.strip()
    if not re.fullmatch(r"https://hc-ping\.com/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError("Expected a protected hc-ping.com UUID URL")
    return value


def backup_recent(directory: Path, now: float) -> bool:
    files = list(directory.glob("*.age"))
    return bool(files) and any(0 <= now - p.stat().st_mtime <= 26 * 3600 for p in files if p.stat().st_size > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ping-file", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        url = endpoint(args.ping_file.read_text(encoding="utf-8-sig"))
        child_env = {**os.environ, "CSP_DRY_RUN": "true", "CSP_DISABLE_BROKER_ACCESS": "true", "CSP_AI_ENABLED": "false"}
        result = subprocess.run(
            [str(args.python.resolve()), "-m", "csp_alerts.cli", "--env-file", str(args.env_file.resolve()), "health"],
            cwd=args.runtime_root.resolve(), env=child_env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        )
        recent = backup_recent(args.backup_directory, datetime.now(UTC).timestamp())
        ok = result.returncode == 0 and recent
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(url if ok else url + "/fail", data=b"", method="POST")
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("Health monitor did not acknowledge ping")
        print(json.dumps({"schema_version": 1, "status": "PASS" if ok else "FAIL",
                          "application_health_ok": result.returncode == 0, "backup_recent": recent}))
        return 0 if ok else 1
    except (OSError, ValueError, subprocess.SubprocessError):
        # The secret URL, subprocess output, and HTTP exception are never printed.
        print('{"schema_version":1,"status":"ERROR","reason":"health probe failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
