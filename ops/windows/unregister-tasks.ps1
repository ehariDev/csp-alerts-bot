<#
.SYNOPSIS
  Remove the csp-alerts scheduled tasks. Preview only unless -Apply.
#>
param([switch]$Apply)
$ErrorActionPreference = "Stop"
$found = Get-ScheduledTask -TaskName "csp-alerts-*" -ErrorAction SilentlyContinue
if (-not $found) { Write-Host "No csp-alerts tasks registered."; exit 0 }
foreach ($t in $found) {
  Write-Host ("{0} ({1})" -f $t.TaskName, $t.State)
  if ($Apply) { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false; Write-Host "    removed" }
}
if (-not $Apply) { Write-Host "PREVIEW ONLY: re-run with -Apply to remove." }
