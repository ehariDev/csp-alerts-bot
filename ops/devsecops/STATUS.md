# Support delivery status — 2026-10-30

Target: `https://github.com/ehariDev/csp-alerts-bot`. The owner's earlier no-push instruction was superseded by authorization to push a feature branch, with **no merge to main**. Remote inspection found an empty, public repository. Publication is limited to DevSecOps-owned `.github/` and `ops/devsecops/` files; no application files, agent instructions, runtime configuration or local reports are included. No task registration, Telegram sends, host configuration changes, production migration or deployment were performed. No pre-existing Claude-owned files were edited by this support work.

## Prepared locally

- Separate locked scanner/build environment, publication privacy checks and sanitized staging, verified upstream checks, checksum-pinned secret scanner, SHA-pinned CI, Dependabot, PR/bug templates and gated candidate-artifact workflow.
- Independent SQLite snapshot/encryption/inspection helper, restricted external health probe, read-only Windows host audit, operations guidance and threat model.
- Application and acceptance tests remain under Claude's ownership. This package adds only its own tests. It does not amend the authoritative requirements or give Claude new instructions.

## Observed evidence

- Support verification: **15 tests passed**, including a native age encrypt/decrypt round trip on a synthetic SQLite journal, refusal to overwrite an existing recovery point, and a Gitleaks regression proving the exact false-positive exception does not hide a synthetic credential on the same line. The backup test verifies the original database is unchanged and temporary plaintext is removed. It does not validate production keys, real data, remote storage or a complete application restore. Native-tool tests explicitly skip when their tools are absent; all ran locally with verified binaries.
- Support Ruff check, workflow SHA policy check, YAML parsing and PowerShell parse checks passed locally. Hosted CI remains unexecuted.
- Locked runtime advisory audit: **34 dependencies, zero known advisories, zero skipped dependencies**.
- Locked support advisory audit: **40 dependencies, zero known advisories, zero skipped dependencies**.
- Gitleaks 8.30.0: checked official binary SHA-256 before execution. The sanitized source snapshot scanned without findings after narrowly triaging a synthetic expression in `test_exits_drawdown.py`. The original unfiltered result was one false positive; the exception is confined to the exact match and file. No live `.env` or database was copied into the snapshot.
- Stockpile checkout: reviewed SHA matches and the independent cleanliness/runtime-source checks pass.
- Wheel build: successful; CLI and SQL migrations are included. Clean installation and a full candidate deployment are still required.
- Source correctness check (`E9,F63,F7,F82`) passed at scan time. An earlier full-suite attempt stopped on a transient `IndentationError` in `runner.py` while the application workspace was changing. Do not treat that attempt as current acceptance evidence or as a defect fixed by this support work.
- Existing application suite, Python 3.14: **1,644 passed, three failed**, in 259.44 seconds. Loopback fake-server tests required the local sandbox restriction to be lifted; external test sockets remained disabled by the project's pytest configuration. The three outcomes are detailed below. Source/config hashes matched the staged snapshot during validation. This is working-tree evidence, not a committed release approval.
- Bandit found **eight medium findings**: seven dynamic SQL construction sites (outbox/store) and one urllib URL-opening site (Telegram transport). These are patterns requiring review, not eight confirmed vulnerabilities. The interpolated constant/placeholder constructions appear lower-risk on source inspection, but no suppression or waiver was applied. CI remains blocked pending application-owner triage/remediation.
- The target is public, contrary to the original private-repository planning default; only the support additions are being published. No root Git repository exists in the workspace. Hosted workflow checks and remote settings need verification on the feature branch. The empty remote has no existing main branch to merge into; this push must not create or update main.

Raw local reports and build outputs are in ignored `_work/`. They are not release approvals. Advisory results are time-specific and do not cover unknown vulnerabilities, the Windows OS, every native library or the uninstalled upstream application's dependency list.

## Acceptance failures for Claude to reconcile

1. `tests/acceptance/test_entry_gates.py::test_snapshot_age_30h[108000-True]`: the test expects a pass at exactly 108,000 seconds; the implementation returns `LEDGER_STALE`. Resolve against the approved current policy/specification; do not loosen a financial gate merely to satisfy an old assertion.
2. `tests/acceptance/test_s3_monitor.py::test_terminal_and_unfilled_states_get_no_open_position_alerts[ASSIGNED]`: the test expects no messages; the implementation emits an urgent assignment-pending-reconciliation review. Resolve the intended supported state and independent oracle. Do not remove an obligation warning solely to turn this green.
3. `tests/acceptance/test_settings_policy_boundary.py::test_repo_contains_no_real_looking_telegram_token_or_secret`: the root-wide test reads the owner's configured, ignored `.env`. It passes in the sanitized publication snapshot, where `.env` is absent. The assertion captured local credential-bearing details into the private test report; those details were removed from the support log/XML. The configured `.env` itself was not changed. Publication still excludes it and the staged secret scan is clean. Claude's tester should reconcile the intended source/working-tree scan boundary without weakening scans of publishable files.

Targeted reproduction from the sanitized snapshot: **two failed, one passed**, confirming the first two failures independently of the local runtime file. No acceptance tests or application files were edited here.

## Handoff to Claude / operator

1. Reconcile the acceptance results above and obtain stable passing unit/independent acceptance evidence. Resolve typed health behavior, runtime upstream/module-origin enforcement, provider-error sanitization and production Telegram URL/redirect restrictions in application-owned files.
2. Triage the eight Bandit results with concrete data-flow evidence. Fix exploitable paths; scope any false-positive exception to reviewed code and record an owner/expiry. Do not globally disable B608 or B310.
3. Review every sender entrypoint, including `telegram-test`, against the shared application lock and crash recovery; an independent static check does not prove concurrency safety.
4. Reconcile current feature/status documentation through its existing owner. Preserve the supported single-paper-account boundary and avoid silent policy changes.
5. Publish only the authorized support additions to `feature/devsecops-readiness`, without creating/updating main, force-pushing, or including local financial data. The application owner must add the application baseline separately before the application CI matrix or release candidate can run. Existing-state application results above remain local evidence, not results from the support-only branch.
6. On the intended runtime host, validate ACLs, encryption, awake/logon behavior, scheduling and private-message receipt. Install age, configure off-machine storage and monitoring, verify encrypted backup/decryption and remote copy, and rehearse restoration. These live gates remain unverified.

The production gate is **NOT READY** until those application and operator gates pass. Future commercial, multi-account, cloud and AI work remains deferred as described in the support README.
