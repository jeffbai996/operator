# Install Operator's real-desktop input broker in the interactive Windows session.
# Per-user only: no elevation, password, or machine-wide service required.
$ErrorActionPreference = "Stop"

$taskName = "OperatorInputBroker"
$installDir = Join-Path $env:LOCALAPPDATA "Operator"
$installedScript = Join-Path $installDir "win_input.ps1"
$launcherSource = Join-Path $PSScriptRoot "start-win-input-broker.vbs"
$installedLauncher = Join-Path $installDir "start-win-input-broker.vbs"
$startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "OperatorInputBroker.lnk"
$onDemandShortcut = Join-Path $installDir "OperatorInputBroker.lnk"
$brokerDir = Join-Path $env:TEMP "operator-input-broker"
$heartbeat = Join-Path $brokerDir "heartbeat.json"
$sourceScript = Join-Path $PSScriptRoot "win_input.ps1"

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
New-Item -ItemType Directory -Path $brokerDir -Force | Out-Null

# Task Scheduler's InteractiveToken process can still land in a window station
# that rejects SetCursorPos. Replace the old task with an Explorer-startup
# launcher, which runs in the logged-in user's actual input desktop.
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
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Remove-Item -LiteralPath $heartbeat -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath $sourceScript -Destination $installedScript -Force
Copy-Item -LiteralPath $launcherSource -Destination $installedLauncher -Force
Remove-Item -LiteralPath $startupShortcut -Force -ErrorAction SilentlyContinue

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($onDemandShortcut)
$shortcut.TargetPath = $installedLauncher
$shortcut.WorkingDirectory = $installDir
$shortcut.WindowStyle = 7
$shortcut.Save()

$startedAtUtc = [DateTime]::UtcNow
# Ask the existing Explorer shell to open the shortcut, rather than spawning a
# child of this installer. That preserves the actual WinSta0 input desktop.
$explorer = Join-Path $env:WINDIR "explorer.exe"
& $explorer $onDemandShortcut

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
  throw "OperatorInputBroker startup launcher did not produce a heartbeat"
}
Write-Output "OperatorInputBroker installed for on-demand launch; no Startup entry."
