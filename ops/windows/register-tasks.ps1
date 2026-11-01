<#
.SYNOPSIS
  Register the csp-alerts Windows Task Scheduler tasks. DRY RUN unless you pass -Apply.

.DESCRIPTION
  Without -Apply this only PRINTS what it would create. Without -Send the registered tasks run the dry-run
  commands (they print to the task history/log and never contact Telegram). -Send adds --send to the
  commands; sends still require CSP_DRY_RUN=false in .env and a pinned private chat (telegram-setup --apply).

  Times are Central time. Task Scheduler uses the PC's local time zone, so this script refuses to run unless
  the PC is set to Central (use -IgnoreTimeZone only if you convert the times yourself).

  market-refresh (Stage A): it NEVER wakes the PC (no -WakeToRun; WakeToRun is explicitly $false). Two triggers on the one
  task: (1) weekdays 07:30-16:30 Central every 5 minutes (covers the 08:30-15:00 CT regular session with margin) and
  (2) one daily 00:00 trigger repeating HOURLY for 23h59m (fires 00:00, 01:00 ... 23:00; the next day's 00:00 trigger
  starts a fresh series, so there is no gap and no double fire at the 23:55/00:00 boundary). Trigger 2 also fires inside
  the weekday window; those firings are harmless because the job decides what is due (a symbol is fetched at most every
  5 minutes while the US regular session is open and at most hourly otherwise, never twice within 60 s, with backoff
  after failures) and MultipleInstances IgnoreNew drops an overlapping start. While the PC sleeps nothing is fetched and
  the dashboard shows the last data as STALE / MARKET-CLOSED. It writes only market-data/run tables (never the ledger,
  snapshots, outbox or Telegram) and has NO send mode, so -Send does not change it and there is no --dry-run on its task
  (a dry run would persist nothing, defeating its purpose). It uses its own lock file. No catch-up after wake (old data
  is simply older; StartWhenAvailable is false).

  Missed-run policy: monitor, reconcile and heartbeat tasks use "start when available" so they run once after
  the PC wakes. Entry scans do NOT (and `scan --scheduled-for` skips a run more than 15 minutes late), so a
  stale entry scan is never replayed. The tasks need you logged in; the PC must be awake (WakeToRun is set on every
  task EXCEPT market-refresh, but a powered-off PC cannot run anything and cannot announce its own outage).

.EXAMPLE
  .\register-tasks.ps1                 # preview only
  .\register-tasks.ps1 -Apply          # register dry-run tasks
  .\register-tasks.ps1 -Apply -Send    # register tasks that send to Telegram
#>
param(
  [switch]$Apply,
  [switch]$Send,
  [switch]$IgnoreTimeZone,
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)
$ErrorActionPreference = "Stop"
$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $RepoRoot ".env"
$mode = if ($Send) { "--send" } else { "--dry-run" }
$prefix = "csp-alerts-"

if (-not (Test-Path $python)) { throw "Python not found at $python. Create the venv first (see RUNBOOK.md)." }
if (-not $IgnoreTimeZone -and (Get-TimeZone).Id -ne "Central Standard Time") {
  throw "PC time zone is '$((Get-TimeZone).Id)', not Central. Fix it or pass -IgnoreTimeZone (and convert the times yourself)."
}
if ($Send -and -not (Test-Path $envFile)) { throw ".env not found at $envFile" }

$weekdays = @("Monday","Tuesday","Wednesday","Thursday","Friday")
function New-CliArgs([string]$cmd) { return "-m csp_alerts.cli --env-file `"$envFile`" $cmd" }

# Name, command arguments, trigger kind, time(s), catch-up after wake?
$tasks = @(
  @{ Name="scan-1005";   Args=(New-CliArgs "scan --scheduled-for 10:05 $mode"); Kind="weekdays"; At="10:05"; CatchUp=$false },
  @{ Name="scan-1305";   Args=(New-CliArgs "scan --scheduled-for 13:05 $mode"); Kind="weekdays"; At="13:05"; CatchUp=$false },
  @{ Name="market-refresh"; Args=(New-CliArgs "market-refresh");                Kind="refresh";  At="07:30"; CatchUp=$false },
  @{ Name="monitor";     Args=(New-CliArgs "monitor $mode");                    Kind="monitor";  At="08:30"; CatchUp=$true  },
  @{ Name="monitor-daily"; Args=(New-CliArgs "monitor $mode");                  Kind="daily";    At="07:30"; CatchUp=$true  },
  @{ Name="reconcile-reminder"; Args=(New-CliArgs "reconcile-reminder $mode");  Kind="weekdays"; At="15:30"; CatchUp=$true  },
  @{ Name="heartbeat";   Args=(New-CliArgs "heartbeat $mode");                  Kind="weekdays"; At="08:00"; CatchUp=$true  }
)

foreach ($t in $tasks) {
  $name = $prefix + $t.Name
  Write-Host ("{0,-34} {1,-9} {2}  catch-up-after-wake={3}" -f $name, $t.Kind, $t.At, $t.CatchUp)
  Write-Host ("    {0} {1}" -f $python, $t.Args)
  if (-not $Apply) { continue }

  $action = New-ScheduledTaskAction -Execute $python -Argument $t.Args -WorkingDirectory $RepoRoot
  switch ($t.Kind) {
    "weekdays" { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $t.At }
    "daily"    { $trigger = New-ScheduledTaskTrigger -Daily -At $t.At }
    "monitor"  {
      # every 15 minutes from 08:30 CT (market open) for 6h45m (to 15:15 CT), weekdays
      $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $t.At
      $rep = (New-ScheduledTaskTrigger -Once -At $t.At -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 45)).Repetition
      $trigger.Repetition = $rep
    }
    "refresh"  {
      # (1) weekdays 07:30-16:30 CT every 5 minutes (duration 9h01m so the 16:30 firing is included)
      $t1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $t.At
      $t1.Repetition = (New-ScheduledTaskTrigger -Once -At $t.At -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 9 -Minutes 1)).Repetition
      # (2) every day, hourly for 23h59m from 00:00 (00:00..23:00); the next day's 00:00 trigger restarts the series: no gap
      $t2 = New-ScheduledTaskTrigger -Daily -At "00:00"
      $t2.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 59)).Repetition
      $trigger = @($t1, $t2)
    }
  }
  if ($t.Kind -eq "refresh") {
    # market-refresh must never wake the PC: no wake flag, and the property is forced off
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
    $settings.WakeToRun = $false
  } else {
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
  }
  if ($t.CatchUp) { $settings.StartWhenAvailable = $true } else { $settings.StartWhenAvailable = $false }
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  Write-Host "    registered $name"
}

if (-not $Apply) {
  Write-Host ""
  Write-Host "PREVIEW ONLY: nothing was registered. Re-run with -Apply to register. Mode: $mode"
} else {
  Write-Host ""
  Write-Host "Registered. Mode: $mode. Verify with: Get-ScheduledTask -TaskName '$prefix*' | Get-ScheduledTaskInfo"
}
