# Paper-pilot threat model

Assets: Telegram bot credential and recipient identity; private journal and exports; approvals, policy and drawdown halt; code/dependency provenance; alert eligibility and delivery history; recovery identity and backup copies.

Trust boundaries: GitHub contribution to CI; CI to candidate artifact; third-party source and public market data to application; operator input to the journal; application to outbound Telegram; local host to encrypted backup storage and external health monitor. No broker connection or inbound Telegram control is permitted.

| Threat | Supporting control | Residual risk / application-owner action |
| --- | --- | --- |
| Secret or financial data committed | Allowlist export, private-path checks, empty whitelist, redacted history/current scans, private repo | Pattern scanners cannot recognize every private field; review the actual staged files before upload |
| Malicious PR or workflow dependency | Hosted ephemeral runners, read-only token, SHA-pinned actions, no production secrets, protected branches | Repository owners and compromised upstream dependencies remain trusted execution sources |
| Modified or spoofed Stockpile | Reviewed SHA, tracked-content and runtime-file checks, archive from Git object | Application currently imports through sys.path; runtime enforcement and imported-module-origin verification belong to Claude |
| Vulnerable dependency | Frozen separate locks, hashed export, advisory scans, weekly updates, SBOM | Advisory gaps, malicious packages, native libraries and platform packages need separate scrutiny |
| Fabricated/stale market observations | Existing fail-closed policy and synthetic acceptance tests | Yahoo remains indicative; never relabel fresh retrieval as verified quote time |
| Ambiguous/duplicate Telegram delivery | Existing durable outbox, UNKNOWN_ACK handling, application job lock | Audit all sender entrypoints and redirect behavior; startup recovery must not race an active sender |
| Journal corruption, tampering or lost PC | SQLite backup API, integrity/hash checks, age encryption, off-machine copies, recovery drills | Audit triggers do not protect against a compromised host; backups do not capture later manual trades |
| Plaintext scratch or recovery key exposure | Encrypted restricted local volume; public recipient only on host; offline private identity | Temporary-file deletion cannot securely erase an unencrypted SSD; operator ACL and key management are critical |
| Host outage or silent missed jobs | External dead-man check, bounded probe, schedule checks and run history | Same-host heartbeat alone is insufficient; independent notification path must be tested |
| Unexpected network egress / token leak | No runtime credentials in CI; fixed host/no-redirect health endpoint; sanitized subprocess output | Application Telegram endpoint/redirect and provider exception handling remain review items |
| Scope expansion into multi-user/commercial service | Single-paper-account release boundary, dependency attribution | Requires license review, tenant isolation, authorization and revised architecture before launch |

Security findings are triaged by exploitability and supported behavior, not by test count. No scanner result implies financial suitability or real-money readiness.
