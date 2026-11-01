"""Tests for our support tools only; no application imports or live credentials."""
from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import inspect_snapshot, snapshot
from health_probe import backup_recent, endpoint
from readiness import check_workflows, private_path, publication_check, verify_upstream
from release import wheel_check
from run_tests import WINDOWS_ONLY, clean_test_env, pytest_command
from stage import stage


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config/approved.json").write_text('{"approved_underlyings": []}')
        (self.root / ".gitignore").write_text(".env\n")

    def test_private_material_and_agent_files_are_not_staged(self):
        (self.root / ".env").write_text("PRIVATE")
        (self.root / "CLAUDE.md").write_text("owner rules")
        (self.root / "config/config.yaml").write_text("private")
        output = self.root / "new-snapshot"
        stage(self.root, output)
        self.assertFalse((output / ".env").exists())
        self.assertFalse((output / "CLAUDE.md").exists())
        self.assertFalse((output / "config/config.yaml").exists())
        self.assertEqual((self.root / ".gitignore").read_text(), ".env\n")
        self.assertIn("*.key", (output / ".gitignore").read_text())

    def test_nonempty_approvals_fail_without_printing_values(self):
        (self.root / "config/approved.json").write_text('[{"private": "OWNER_VALUE"}]')
        report = publication_check(self.root)
        self.assertEqual(report["status"], "FAIL")
        self.assertNotIn("OWNER_VALUE", json.dumps(report))

    def test_destination_never_overwritten(self):
        with self.assertRaises(ValueError):
            stage(self.root, self.root / "config")

    def test_manifest_records_separate_paths_from_hashes(self):
        output = self.root / "snapshot"
        stage(self.root, output)
        manifest = json.loads((output / "PUBLICATION_MANIFEST.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertTrue(manifest["files"])
        self.assertEqual(set(manifest["files"][0]), {"path", "sha256"})

    def test_tracked_secrets_rejected_even_outside_allowlist(self):
        (self.root / ".git").mkdir()
        with patch("readiness.git", return_value="other/private.key\0.env\0"):
            self.assertEqual(publication_check(self.root)["status"], "FAIL")

    def test_private_paths(self):
        for name in [".env.production", "data/export.csv", "private.key", "config/config.yaml", "x.db-wal"]:
            self.assertTrue(private_path(Path(name)), name)
        self.assertFalse(private_path(Path(".env.example")))

    def test_workflow_unpinned_action_rejected(self):
        folder = self.root / ".github/workflows"
        folder.mkdir(parents=True)
        (folder / "test.yml").write_text("steps:\n  - uses: actions/checkout@main\n")
        self.assertEqual(check_workflows(self.root)["status"], "FAIL")

    def test_workflow_commented_examples_are_not_executed_actions(self):
        folder = self.root / ".github/workflows"
        folder.mkdir(parents=True)
        (folder / "test.yml").write_text(
            "# - uses: actions/setup-example@v1\n"
            "steps:\n  - uses: actions/checkout@" + "a" * 40 + "\n"
        )
        self.assertEqual(check_workflows(self.root)["status"], "PASS")

    def test_upstream_wrong_revision_and_dirty_worktree_rejected(self):
        with patch("readiness.git", return_value="wrong"):
            with self.assertRaises(ValueError):
                verify_upstream(self.root)
        from readiness import UPSTREAM_SHA
        with patch("readiness.git", side_effect=[UPSTREAM_SHA, " M chain.py"]):
            with self.assertRaises(ValueError):
                verify_upstream(self.root)


class BackupTests(unittest.TestCase):
    def test_consistent_backup_and_tamper_detection_without_live_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "live.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE audit (value TEXT)")
                connection.execute("INSERT INTO audit VALUES ('HALT')")
                connection.commit()
            original = db.read_bytes()
            policy, approved = root / "policy.yaml", root / "approved.json"
            policy.write_text("dry_run: true")
            approved.write_text("[]")
            target = root / "snapshot.zip"
            snapshot(db, policy, approved, target)
            self.assertEqual(inspect_snapshot(target, root)["status"], "PASS")
            self.assertEqual(db.read_bytes(), original)
            broken = root / "tampered.zip"
            with zipfile.ZipFile(target) as source, zipfile.ZipFile(broken, "w") as dest:
                for name in source.namelist():
                    dest.writestr(name, b"changed" if name == "policy.yaml" else source.read(name))
            with self.assertRaises(ValueError):
                inspect_snapshot(broken, root)

    def test_archive_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "malicious.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../outside", "bad")
            with self.assertRaises(ValueError):
                inspect_snapshot(path, root)
            self.assertFalse((root.parent / "outside").exists())

    def test_missing_database_cannot_create_empty_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                snapshot(root / "missing", root / "policy", root / "approved", root / "backup.zip")
            self.assertFalse((root / "missing").exists())


class WheelTests(unittest.TestCase):
    def test_missing_migrations_block_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "bad.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("csp_alerts/cli.py", "")
            with self.assertRaises(ValueError):
                wheel_check(wheel)


class HealthTests(unittest.TestCase):
    def test_endpoint_rejects_redirects_userinfo_and_arbitrary_hosts(self):
        valid = "https://hc-ping.com/00000000-0000-0000-0000-000000000000"
        self.assertEqual(endpoint(valid), valid)
        for value in [valid + "?redirect=https://elsewhere", valid.replace("https:", "http:"),
                      valid.replace("hc-ping.com", "hc-ping.com.evil.example"), "https://localhost/private"]:
            with self.assertRaises(ValueError):
                endpoint(value)

    def test_missing_stale_empty_or_future_backups_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(backup_recent(root, 0))
            p = root / "backup.age"
            p.write_bytes(b"")
            now = p.stat().st_mtime
            self.assertFalse(backup_recent(root, now))
            p.write_bytes(b"encrypted sample")
            now = p.stat().st_mtime
            self.assertTrue(backup_recent(root, now + 60))
            self.assertFalse(backup_recent(root, now + 27 * 3600))
            self.assertFalse(backup_recent(root, now - 60))


class TestRunnerTests(unittest.TestCase):
    def test_ci_application_settings_and_credentials_cannot_override_fixture_files(self):
        original = {"CSP_DRY_RUN": "true", "CSP_DISABLE_BROKER_ACCESS": "true",
                    "TELEGRAM_BOT_TOKEN": "synthetic", "csp_dashboard_port": "9999", "PATH": "tools"}
        self.assertEqual(clean_test_env(original), {"PATH": "tools"})
        self.assertEqual(original["CSP_DRY_RUN"], "true")

    def test_windows_keeps_all_tests_and_every_platform_blocks_external_sockets(self):
        for platform in ("win32", "linux"):
            command = pytest_command(["tests/unit", "tests/acceptance", "-q"], platform)
            self.assertIn("--disable-socket", command)
            self.assertIn("--allow-hosts=127.0.0.1,::1", command)
            exclusions = [arg for arg in command if arg.startswith("--deselect=")]
            self.assertEqual(len(exclusions), 0 if platform == "win32" else len(WINDOWS_ONLY))
            self.assertFalse(any("test_owner_journey" in arg or "real_process_start_stop" in arg for arg in exclusions))


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "native age tools not installed")
class EncryptedBackupTests(unittest.TestCase):
    def test_native_age_roundtrip_keeps_live_database_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "synthetic-identity.txt"
            subprocess.run([shutil.which("age-keygen"), "-o", str(identity)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            recipient = subprocess.run([shutil.which("age-keygen"), "-y", str(identity)],
                                       check=True, capture_output=True, text=True).stdout.strip()
            db = root / "synthetic.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE audit (value TEXT)")
                connection.execute("INSERT INTO audit VALUES ('HALT')")
                connection.commit()
            original = db.read_bytes()
            (root / "policy.yaml").write_text("dry_run: true")
            (root / "approved.json").write_text("[]")
            backup_script = Path(__file__).resolve().parents[1] / "backup.py"
            created = subprocess.run([
                sys.executable, str(backup_script), "create", "--database", str(db),
                "--policy", str(root / "policy.yaml"), "--approvals", str(root / "approved.json"),
                "--scratch", str(root), "--output", str(root / "test.age"), "--recipient", recipient,
            ], check=True, capture_output=True, text=True)
            self.assertTrue(json.loads(created.stdout)["encrypted"])
            existing = (root / "test.age").read_bytes()
            duplicate = subprocess.run([
                sys.executable, str(backup_script), "create", "--database", str(db),
                "--policy", str(root / "policy.yaml"), "--approvals", str(root / "approved.json"),
                "--scratch", str(root), "--output", str(root / "test.age"), "--recipient", recipient,
            ], capture_output=True, text=True)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual((root / "test.age").read_bytes(), existing)
            inspected = subprocess.run([
                sys.executable, str(backup_script), "inspect", "--input", str(root / "test.age"),
                "--identity-file", str(identity), "--scratch", str(root),
            ], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(inspected.stdout)["status"], "PASS")
            self.assertEqual(db.read_bytes(), original)
            self.assertFalse(any(root.glob("csp-backup-*")))


@unittest.skipUnless(shutil.which("gitleaks"), "native Gitleaks not installed")
class SecretScanTests(unittest.TestCase):
    def test_exact_synthetic_exception_does_not_hide_a_secret_on_same_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "tests/unit/test_exits_drawdown.py"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("base = dict({0}=p.{0}, {1}=NOW - one)\n".format("contract_key", "retrieved_at_utc"))
            config = Path(__file__).resolve().parents[1] / "gitleaks.toml"
            command = [shutil.which("gitleaks"), "dir", str(root), "--config", str(config),
                       "--redact=100", "--no-banner"]
            clean = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(clean.returncode, 0)
            # Generated synthetic PAT-shaped string; never a real credential.
            fixture.write_text(fixture.read_text().rstrip() + '; token = "' + "ghp_" + secrets.token_hex(18) + '"\n')
            detected = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(detected.returncode, 1)


if __name__ == "__main__":
    unittest.main()
