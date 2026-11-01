"""Run existing tests with isolated application settings and explicit platform scope.

The complete suite is required on Windows. Linux also runs the suite, omitting
only the enumerated Windows path/case/junction and Windows PowerShell checks.
Other failures remain failures, including Linux dashboard restart behavior.
No application code, fixture clock, safety policy or test assertion is changed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

WINDOWS_ONLY = (
    "tests/acceptance/test_s3_hardening.py::test_backup_refuses_the_live_database_path_in_every_spelling",
    "tests/acceptance/test_s3_hardening.py::test_backup_refuses_hard_link_and_mixed_case_directory_alias",
    "tests/acceptance/test_s3_hardening.py::test_backup_refuses_a_directory_symlink_or_junction_path_to_the_live_database",
    "tests/acceptance/test_s3_status_windows_backup.py::test_powershell_scripts_parse_without_errors_and_are_not_executed",
)


def clean_test_env(environ: Mapping[str, str]) -> dict[str, str]:
    # Fixtures intentionally exercise both true/false settings in temporary .env
    # files. Parent CI settings must not override them or inject live credentials.
    return {key: value for key, value in environ.items()
            if not key.upper().startswith(("CSP_", "TELEGRAM_"))}


def pytest_command(args: Sequence[str], platform: str) -> list[str]:
    command = [sys.executable, "-m", "pytest", *args,
               "--disable-socket", "--allow-hosts=127.0.0.1,::1"]
    if platform != "win32":
        command.extend(f"--deselect={node}" for node in WINDOWS_ONLY)
    return command


def main() -> int:
    args = sys.argv[1:] or ["tests/unit", "tests/acceptance", "-q", "--tb=short"]
    if sys.platform != "win32":
        print("Linux compatibility scope: Windows path/case/junction and Windows PowerShell cases run on Windows only.",
              flush=True)
    return subprocess.run(pytest_command(args, sys.platform), env=clean_test_env(os.environ), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
