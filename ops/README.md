# Operations (Windows first)

The Windows scheduling scripts exist and are documented in [../RUNBOOK.md](../RUNBOOK.md) section 9.

- `ops/windows/register-tasks.ps1`: previews the Task Scheduler tasks (scan 10:05 and 13:05 CT, monitor every 15 min from 08:30 CT, daily 07:30 calendar run, heartbeat 08:00, reconcile reminder 15:30). `-Apply` registers them (dry-run commands); `-Apply -Send` registers tasks that send (also needs `CSP_DRY_RUN=false` and a pinned private chat). Refuses unless the PC time zone is Central. **The `-Apply` path has not been executed yet** (preview and script parse only).
- `ops/windows/unregister-tasks.ps1`: previews/removes the tasks (`-Apply`).
- Missed runs: monitor/reconcile/heartbeat catch up after wake; entry scans are never replayed (`scan --scheduled-for` skips a run more than 15 minutes late). One job at a time (file lock).
- The PC must be on and logged in; a stopped PC cannot announce its own outage.
- Backup: `journal backup` (SQLite online backup). Restore is a manual file copy followed by `doctor` (see the RUNBOOK and `tests/unit/test_journey.py`).

Linux alternatives are not packaged: use `systemd` timers or `cron` with `CRON_TZ=America/Chicago` calling the same CLI commands, one job at a time (the application lock also applies), and test the DST behaviour yourself.
