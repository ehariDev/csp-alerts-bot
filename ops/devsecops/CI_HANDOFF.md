# CI diagnosis — 2026-11-01

Reference: Readiness run 41325287732 (PR #5), commit 397c893. CodeQL and support checks passed; application and security jobs failed. Main remains protected. This change does not alter application code, existing application/acceptance tests, policy, or required branch checks.

## CI-owned corrections

- Run pytest in a child environment without inherited `CSP_*`/`TELEGRAM_*` values. Workflow-level safety defaults previously overrode tests' temporary `.env` files, defeating the false-broker-flag test and the fake Telegram send-mode test. External sockets remain disabled, with loopback as the only exception.
- Keep the complete suite on both Windows Python versions. Linux omits only four explicitly enumerated Windows-only functions (five parametrized cases): backslash paths, case-insensitive paths, `cmd` junctions and `powershell.exe` parsing. Linux dashboard restart, security and all other tests remain enforced.
- Increase application timeout from 20 to 40 minutes. The Windows 3.12 run took 19m19s before checkout/install overhead; Windows 3.14 was canceled at the old deadline.
- Store publication manifest entries as `path`/`sha256` records. The previous filename-as-key map was flagged as a credential assignment followed by a high-entropy hash. The full snapshot is still scanned; no manifest-file exclusion or new secret allowlist was added.
- Collect independent security outcomes before failing the final security gate, so a scanner failure no longer prevents dependency and Bandit checks from reporting their own findings. Every previously required security failure still fails the job.

Local validation: 19 support tests passed with both native tools available; Ruff, workflow YAML validation, and the full staged snapshot Gitleaks scan passed. The two original configuration failures passed through the fixed runner with the original CI environment reproduced. These targeted checks do not claim the full application suite passes.

## Remaining application-owner work

1. `tests/unit/test_journey.py::test_owner_journey_reconcile_scan_record_monitor_close` still fails in isolation with `NO_TRADE`. The journey uses `datetime.now()`/`date.today()` but `chain_fixtures.raw_row()` defaults its last-trade timestamp from `factories.NOW` (2026-10-30). Resolve the fixture's inconsistent clock through its existing owner; do not change stale-market-data policy or freeze CI's clock to make this pass.
2. `tests/acceptance/test_web_boundary_and_cli.py::test_real_process_start_stop_leaves_journal_and_job_state_untouched` fails on both Linux versions when restarting the process on the same port. `DashboardServer.allow_reuse_address=False` and POSIX socket lifecycle are relevant. The application owner must reconcile the restart contract and exclusive-bind security posture. The test remains required on Linux.
3. Current Bandit analysis reports 11 medium patterns: B608 at `alerts/outbox.py:132`, `journal/store.py:162,230,378,643,650,651,703,722`, and `journal/watchlist.py:75`; B310 at `alerts/telegram.py:67`. Investigate each data flow and fix exploitable behavior or document narrowly scoped, evidence-backed false positives with owner/expiry. None are waived by this CI change. The earlier CI scan stopped before Bandit because of the manifest finding, so its final security failure was not evidence that Bandit actually ran.

PR #4 contains dependency upgrades and remains independent of these CI changes. PR #5 contains CodeQL targeting/pinning plus these CI fixes. Neither PR is approved or merged. CI cannot be described as green until remaining application and security gates pass on the actual PR revision.
