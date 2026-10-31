# DevSecOps support package

Target repository: https://github.com/ehariDev/csp-alerts-bot. The owner authorized a feature-branch push and explicitly prohibited merging to `main`.

The remote was empty and public when inspected. This initial branch contains only the DevSecOps-owned `.github/` and `ops/devsecops/` additions. Application files, Claude instructions, runtime configuration, private data and local evidence are excluded. Application checks explicitly skip until the application baseline is present; skipped checks are not a production-readiness approval. Release packaging refuses to run without that baseline.

This directory and the new `.github/` files are Codex-owned support additions. All pre-existing application code, tests, policy, requirements, documentation, runbooks, `CLAUDE.md`, and `.claude/` remain owned by their existing authors. These tools do not import the application or load its `.env`, except the explicitly invoked health probe, which calls the existing CLI with output discarded. No agents, skills, or competing application scheduler are added.

## Local verification

From the workspace root, using the existing uv executable (or replace `.venv/Scripts/uv.exe` with `uv` on PATH in a clean checkout):

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'ops/devsecops/_work/uv-cache'
.venv/Scripts/uv.exe sync --project ops/devsecops --frozen --python 3.12
ops/devsecops/.venv/Scripts/python.exe -m unittest discover -s ops/devsecops/tests -v
ops/devsecops/.venv/Scripts/ruff.exe check ops/devsecops
ops/devsecops/.venv/Scripts/python.exe ops/devsecops/readiness.py check-workflows
ops/devsecops/.venv/Scripts/python.exe ops/devsecops/readiness.py verify-upstream
ops/devsecops/.venv/Scripts/python.exe ops/devsecops/readiness.py publication-check --output ops/devsecops/_work/publication.json
```

The support environment and lockfile are separate from Claude's dependencies. `_work/` holds caches, reports, binaries and private local artifacts; its contents must never be committed or shared indiscriminately. Source scans, database checks, and checksums are evidence, not a guarantee that the host or dependencies are safe.

## Publication preparation, without touching the working repository

`stage.py NEW_DIRECTORY` creates an explicit allowlist snapshot. It excludes live `.env` files, local config, databases, data, caches, environments, upstream checkouts, and agent instructions. It requires an empty publication whitelist, rejects links, and refuses to overwrite a destination. Only the copied `.gitignore` receives additional protective rules; the workspace `.gitignore` is untouched. It also produces a hash manifest. Review it before initializing Git or publishing. CLAUDE instructions and `.claude/` are deliberately outside this export; this is a clean publication snapshot, not a migration of Claude's working environment.

```powershell
ops/devsecops/.venv/Scripts/python.exe ops/devsecops/stage.py ops/devsecops/_work/new-publication
ops/devsecops/.venv/Scripts/python.exe ops/devsecops/tooling.py
ops/devsecops/_work/bin/gitleaks.exe dir ops/devsecops/_work/new-publication --config ops/devsecops/gitleaks.toml --redact=100 --no-banner
```

`tooling.py` downloads the fixed Gitleaks 8.30.0 release from its official repository and checks an embedded SHA-256 before reading the executable. The only scanner exception is a reviewed, exact synthetic Python expression in one test; it does not allowlist the test directory or credential formats. Never disable redaction for routine scans.

No Git repository is initialized at the root of Claude's workspace. Feature-branch publication uses an isolated checkout under ignored `_work/`. No credentials or financial records are uploaded. Remote settings and branch protections are not applied by these scripts.

## GitHub controls prepared

- `Readiness` runs on PRs, main pushes, manual dispatch, and weekly. It checks the support tools, four OS/Python application combinations, upstream provenance, publication privacy, source correctness, Bandit, redacted Gitleaks history/current scans, and audits both locked dependency sets. No production secrets or self-hosted runners are used. Application failures and medium/high Bandit findings block the gate; audit failures are not waived.
- `Release candidate` reuses every readiness gate, builds a wheel with the separately locked build tools, verifies CLI/migrations, exports hashed runtime requirements and CycloneDX SBOM, and packages a clean committed source snapshot with a Git-object archive of the exact Stockpile revision. It uploads a checksummed candidate artifact only. It does not publish a GitHub release, deploy, enable sending, or approve production use.
- Dependabot proposes weekly uv and action updates; no automatic merges. Actions are pinned to verified full SHAs, and checkout credentials are not retained.

After publication is authorized, configure a private repository, MFA, private vulnerability reporting if supported, read-only default workflow permissions, branch deletion/force-push protection, and required `Support checks`, `Security gates`, and all four application jobs. Keep main protected through PRs. For a solo maintainer, require passing checks without an impossible second-person approval rule. Paid GitHub security features are optional; the CLI gates remain mandatory. No CODEOWNERS identity is invented.

Do not merge a dependency update until tests and scans pass. Critical/high advisories block release. Any accepted lower-risk finding must document reachability, mitigation, owner and expiry; blanket Bandit or secret-scanner exclusions are prohibited. The current Bandit findings have not been waived.

## Windows pilot infrastructure

Use Claude's existing `ops/windows/` scheduler and `RUNBOOK.md`; do not install a second scheduler. The existing interactive tasks require the owner logged in. A dedicated standard-user Windows session is the initial runtime; boot-without-login service operation remains a future application/operations change, not a verified capability.

Provision a supported, always-on Windows host with local NTFS, BitLocker, restricted ACLs, Central time, automatic clock synchronization, normal firewall protection, current patches and an explicit awake/power policy. Use separate release, configuration, data, backup and scratch directories outside OneDrive/network shares. Runtime users can read code and write only operational state; never run jobs as Administrator or SYSTEM. Keep at least 5 GB free, and ensure console output containing financial information is not published as telemetry.

`windows/host-audit.ps1 -RuntimeRoot PATH` is read-only and returns booleans. `REVIEW` or null means a check is unresolved, not passed. BitLocker queries may require separate administrator verification. It does not register tasks, change ACLs, create accounts or alter machine settings.

Deployment procedure: retain the previous immutable release; verify candidate checksum and manifest against the reviewed GitHub artifact; install its wheel with the bundled hashed requirements into a new environment; extract the pinned upstream snapshot to that release's `third_party/stockpile`; supply private paths and credentials locally. Stop scheduled jobs, take a verified backup, then initialize/migrate the new application through the existing CLI. Test default dry-run, financial blocks, health, and the manual paper journey before switching task paths. Never combine a code rollback with an incompatible newer schema; restore a tested pre-migration backup when needed, then reconcile all events since that backup. Actual installation and activation are operator gates, not performed here.

### Encrypted off-machine backup

Install and verify the official [age tools](https://github.com/FiloSottile/age/releases). Generate the recovery identity off-machine; place only its public `age1...` recipient on the runtime host. Keep two protected recovery copies separately from the host and backup storage. Use BitLocker and restrictive ACLs for plaintext scratch; do not use shared TEMP. File deletion is not secure erasure on an unencrypted disk.

```powershell
# Example paths only. Supply your public recipient, not the recovery identity.
python ops/devsecops/backup.py create --database C:/CSP/data/journal.sqlite3 --policy C:/CSP/config/policy.yaml --approvals C:/CSP/config/approved.json --scratch C:/CSP/scratch --output E:/CSP-backups/NEW-UNIQUE-NAME.age --recipient PUBLIC_AGE_RECIPIENT
# Run during a recovery drill on a protected recovery machine:
python ops/devsecops/backup.py inspect --input E:/CSP-backups/NAME.age --identity-file E:/offline-recovery/identity.txt --scratch C:/CSP-recovery/scratch
```

The helper uses SQLite online backup in read-only source mode, checks integrity, snapshots policy/approvals, hashes each archive member, and encrypts before writing the final backup. It never includes `.env`. Inspection rejects unknown/duplicate/path-traversal members, validates hashes and SQLite integrity, and never restores over a live database. The decrypt/inspection process needs enough protected scratch space for the backup. Native age is an explicit prerequisite; there is no plaintext fallback.

Schedule daily backups and take another after confirmed owner mutations through an operations wrapper or manual procedure. Upload only completed ciphertext to owner-controlled off-machine storage, verify its checksum there, and retain 30 daily/12 monthly recovery points. Automated post-mutation hooks, remote retention and remote-copy verification require operator integration; they are not silently inserted into Claude's CLI. Quarterly, restore to an isolated recovery directory, start in dry-run without production secrets, verify audit history/halt and reconcile outbox/ledger before re-enabling delivery. Target RTO: four hours; RPO: latest verified off-machine copy. Measure both in drills.

### Independent outage detection

`health_probe.py` calls the existing `health` command with a 60-second deadline and discards its output. It also requires a nonempty encrypted backup no older than 26 hours. It sends only success/failure to an explicitly configured `hc-ping.com` UUID URL; redirects and other hosts are rejected. It never forwards financial details, exception text or the secret URL.

Create a Healthchecks.io check with five-minute period and 15-minute grace, and owner notifications through an independent channel. Store its secret URL in an ACL-restricted file outside the repository. Add a separately named five-minute probe task under the runtime account; its action calls:

```powershell
python ops/devsecops/health_probe.py --python C:/CSP/releases/RELEASE/.venv/Scripts/python.exe --runtime-root C:/CSP/releases/RELEASE --env-file C:/CSP/config/.env --ping-file C:/CSP/config/healthcheck-url.txt --backup-directory E:/CSP-backups
```

No monitoring account, alert destination, backup storage or probe task is provisioned yet. Current CLI health returns a coarse result; typed differentiation of expected financial blocks versus service failure belongs to Claude's application backlog. A recent local ciphertext file proves neither remote upload nor decryptability; independent remote checks and recovery drills remain required.

## Future enhancements and release gates

Keep the first release personal, single-paper-account and advisory only. Preserve the existing provider/transport seams and pure policy engine. Add typed machine-readable health, runtime upstream checks, stronger transport restrictions and complete restoration workflow through Claude's application ownership. Require licensing review before commercial use and explicit tenant isolation before multiple accounts/users. Move to managed storage/queues and leader election before multiple active workers. Select a cloud provider and recovery objectives before building cloud IaC; Kubernetes is not needed for this pilot. AI remains disabled and may never override policy.

Production remains gated on Claude's passing application/acceptance evidence, resolution of security findings, an owner-confirmed private Telegram test, verified task registration, encrypted off-machine backup/recovery drill, independent outage notification, and a dry-run deployment rehearsal. See `STATUS.md` for observed results and `THREAT_MODEL.md` for the remaining attack surface.
