# win_input.ps1 — inject one computer-use action into the live Windows session.
# Called from WSL via powershell.exe. The agentic loop in loop.py decides the
# action; this script is the thin Win32 executor (SetCursorPos + mouse_event +
# SendKeys), the Windows counterpart to xdotool.
#
# Args: -Action <move|left_click|right_click|double_click|type|key|hotkey|scroll>
#       -X <int> -Y <int>            (for move/click/scroll)
#       -Text <string>               (for type)
#       -Key <string>                (for key — SendKeys syntax, e.g. {ENTER};
#                                     for hotkey — a raw '+'-combo, e.g. win+ctrl+right)
#       -Amount <int>                (for scroll; +up / -down)
#
# `hotkey` exists because SendKeys cannot express the Windows key AT ALL —
# any Win+… shortcut (switch virtual desktop, Win+D, …) needs real
# keybd_event presses with virtual-key codes (found live 2026-07-11: the
# agent "switched desktops", but the Win modifier was silently dropped).
#
# INVASIVE: this moves the owner's real cursor and types into his real session.
param(
  [Parameter(Mandatory=$true)][string]$Action,
  [int]$X = -1, [int]$Y = -1,
  [string]$Text = "", [string]$Key = "", [int]$Amount = 3
)
Add-Type -AssemblyName System.Windows.Forms
$sig = @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);
'@
$U = Add-Type -MemberDefinition $sig -Name U -Namespace W -PassThru
$LEFTDOWN=0x02; $LEFTUP=0x04; $RIGHTDOWN=0x08; $RIGHTUP=0x10; $WHEEL=0x0800

function Move-To($px,$py) { if ($px -ge 0 -and $py -ge 0) { [void]$U::SetCursorPos($px,$py) } }
function Click-Left  { $U::mouse_event($LEFTDOWN,0,0,0,0);  $U::mouse_event($LEFTUP,0,0,0,0) }
function Click-Right { $U::mouse_event($RIGHTDOWN,0,0,0,0); $U::mouse_event($RIGHTUP,0,0,0,0) }

# name → virtual-key code (hotkey path). Single chars resolve via ToUpper;
# F1..F24 via the pattern rule below.
$VKMAP = @{ "ctrl"=0x11; "control"=0x11; "alt"=0x12; "shift"=0x10;
  "win"=0x5B; "windows"=0x5B; "super"=0x5B; "meta"=0x5B; "cmd"=0x5B;
  "left"=0x25; "up"=0x26; "right"=0x27; "down"=0x28;
  "enter"=0x0D; "return"=0x0D; "esc"=0x1B; "escape"=0x1B; "tab"=0x09;
  "space"=0x20; "home"=0x24; "end"=0x23; "page_up"=0x21; "pgup"=0x21;
  "page_down"=0x22; "pgdn"=0x22; "delete"=0x2E; "backspace"=0x08 }
# arrows/nav/del are EXTENDED keys — without the flag NumLock state can turn
# them into numpad digits.
$VKEXT = @(0x25,0x26,0x27,0x28,0x24,0x23,0x21,0x22,0x2D,0x2E)

function Resolve-VK([string]$name) {
  $n = $name.Trim().ToLower()
  if ($VKMAP.ContainsKey($n)) { return [byte]$VKMAP[$n] }
  if ($n.Length -eq 1) { return [byte][char]::ToUpper($n[0]) }
  if ($n -match '^f(\d{1,2})$') { return [byte](0x6F + [int]$Matches[1]) }
  throw "unknown key: $name"
}

function Send-Hotkey([string]$combo) {
  $vks = @($combo -split '\+' | ForEach-Object { Resolve-VK $_ })
  foreach ($vk in $vks) {                     # press in order…
    $fl = if ($VKEXT -contains [int]$vk) { [uint32]0x1 } else { [uint32]0x0 }
    $U::keybd_event($vk, 0, $fl, 0); Start-Sleep -Milliseconds 15
  }
  [array]::Reverse($vks)                      # …release in reverse
  foreach ($vk in $vks) {
    $fl = if ($VKEXT -contains [int]$vk) { [uint32]0x3 } else { [uint32]0x2 }
    $U::keybd_event($vk, 0, $fl, 0); Start-Sleep -Milliseconds 15
  }
}

switch ($Action) {
  "move"         { Move-To $X $Y }
  "left_click"   { Move-To $X $Y; Click-Left }
  "right_click"  { Move-To $X $Y; Click-Right }
  "double_click" { Move-To $X $Y; Click-Left; Start-Sleep -Milliseconds 60; Click-Left }
  "type"         { [System.Windows.Forms.SendKeys]::SendWait($Text) }
  "key"          { [System.Windows.Forms.SendKeys]::SendWait($Key) }
  "hotkey"       { Send-Hotkey $Key }
  "scroll"       { Move-To $X $Y; $U::mouse_event($WHEEL,0,0,[uint32]($Amount*120),0) }
  default        { Write-Error "unknown action: $Action"; exit 2 }
}
Write-Output "ok"
