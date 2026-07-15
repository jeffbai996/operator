# Register Operator's real-desktop input broker in the interactive Windows session.
# Per-user only: no elevation, password, or machine-wide service required.
$ErrorActionPreference = "Stop"

$taskName = "OperatorInputBroker"
$installDir = Join-Path $env:LOCALAPPDATA "Operator"
$installedScript = Join-Path $installDir "win_input.ps1"
$brokerDir = Join-Path $env:TEMP "operator-input-broker"
$heartbeat = Join-Path $brokerDir "heartbeat.json"
$sourceScript = Join-Path $PSScriptRoot "win_input.ps1"

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
New-Item -ItemType Directory -Path $brokerDir -Force | Out-Null

# Registration with -Force updates the task definition but does not replace an
# already-running instance. Stop it first so this install actually loads the
# script copied below, and remove its heartbeat so it cannot satisfy the health
# check for the replacement process.
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  $stopDeadline = [DateTime]::UtcNow.AddSeconds(8)
  do {
    Start-Sleep -Milliseconds 100
    $state = (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
  } while ($state -eq "Running" -and [DateTime]::UtcNow -lt $stopDeadline)
  if ($state -eq "Running") {
    throw "OperatorInputBroker did not stop before reinstall"
  }
}
Remove-Item -LiteralPath $heartbeat -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath $sourceScript -Destination $installedScript -Force

$arguments = ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass ' +
              '-File "{0}" -BrokerDir "{1}"' -f $installedScript, $brokerDir)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Operator real-desktop input broker (interactive user session)." |
    Out-Null
$startedAtUtc = [DateTime]::UtcNow
Start-ScheduledTask -TaskName $taskName

$deadline = [DateTime]::UtcNow.AddSeconds(8)
while ([DateTime]::UtcNow -lt $deadline) {
  if (Test-Path $heartbeat) {
    $heartbeatTime = (Get-Item -LiteralPath $heartbeat).LastWriteTimeUtc
    if ($heartbeatTime -ge $startedAtUtc) { break }
  }
  Start-Sleep -Milliseconds 100
}
if (-not (Test-Path $heartbeat) -or
    (Get-Item -LiteralPath $heartbeat).LastWriteTimeUtc -lt $startedAtUtc) {
  throw "OperatorInputBroker registered but did not produce a heartbeat"
}
Write-Output "OperatorInputBroker installed and running."
