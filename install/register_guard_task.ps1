# Registers the fleet memory guard as a scheduled task: every 5 minutes, only while the user
# is logged on (its idle reading needs the logon session), no console window. Opt-in: it may
# close the browser when the user is away, so quick_install does not run this.
#   powershell -NoProfile -File install\register_guard_task.ps1             register or update
#   powershell -NoProfile -File install\register_guard_task.ps1 -Unregister remove
param([switch]$Unregister)

$ErrorActionPreference = 'Stop'
$name = 'Epoch memory guard'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed: $name"
    exit 0
}

$fleet = Join-Path $HOME '.claude\tools\fleet\fleet.py'
if (-not (Test-Path $fleet)) { throw "not installed: $fleet (run install/sync_global.py --apply)" }
$pyw = (Get-Command pyw.exe -ErrorAction Stop).Source

$action = New-ScheduledTaskAction -Execute $pyw -Argument "-3.13 `"$fleet`" guard run"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Output "registered: $name (every 5 minutes while logged on)"
