<# Read-only host audit. Does not set ACLs, change power settings, register tasks, or load .env. #>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$RuntimeRoot)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$path = [IO.Path]::GetFullPath($RuntimeRoot)
$drive = [IO.Path]::GetPathRoot($path)
$volume = Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DeviceID -eq $drive.TrimEnd('\') }
$encrypted = $null
try {
    $bitlocker = Get-BitLockerVolume -MountPoint $drive -ErrorAction Stop
    $encrypted = ($bitlocker.ProtectionStatus -eq 'On')
} catch { # Standard users may not have permission to query BitLocker.
    $encrypted = $null
}
$aclRestricted = $null
if (Test-Path -LiteralPath $path) {
    $acl = Get-Acl -LiteralPath $path
    # Check broad principals by SID; user/group display names vary with Windows language.
    $broad = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
    $aclRestricted = $true
    foreach ($entry in $acl.Access) {
        try { $sid = $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { $aclRestricted = $false; continue }
        if ($sid -in $broad -and $entry.AccessControlType -eq 'Allow') { $aclRestricted = $false }
    }
}
[ordered]@{
    schema_version = 1
    elevated_runtime = $admin
    central_timezone = ((Get-TimeZone).Id -eq 'Central Standard Time')
    local_ntfs = ($volume.DriveType -eq 3 -and $volume.FileSystem -eq 'NTFS')
    free_space_over_5gb = ($volume.FreeSpace -gt 5GB)
    bitlocker_protected = $encrypted
    runtime_acl_restricted = $aclRestricted
    status = if (-not $admin -and $encrypted -eq $true -and $aclRestricted -eq $true -and
                 $volume.DriveType -eq 3 -and $volume.FileSystem -eq 'NTFS' -and
                 $volume.FreeSpace -gt 5GB -and (Get-TimeZone).Id -eq 'Central Standard Time') { 'PASS' } else { 'REVIEW' }
} | ConvertTo-Json
