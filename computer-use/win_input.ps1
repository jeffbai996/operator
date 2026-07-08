# win_input.ps1 — inject one computer-use action into the live Windows session.
# Called from WSL via powershell.exe. The agentic loop in loop.py decides the
# action; this script is the thin Win32 executor (SetCursorPos + mouse_event +
# SendKeys), the Windows counterpart to xdotool.
#
# Args: -Action <move|left_click|right_click|double_click|type|key|scroll>
#       -X <int> -Y <int>            (for move/click/scroll)
#       -Text <string>               (for type)
#       -Key <string>                (for key — SendKeys syntax, e.g. {ENTER})
#       -Amount <int>                (for scroll; +up / -down)
#
# INVASIVE: this moves the owner's real cursor and types into their real session.
param(
  [Parameter(Mandatory=$true)][string]$Action,
  [int]$X = -1, [int]$Y = -1,
  [string]$Text = "", [string]$Key = "", [int]$Amount = 3
)
Add-Type -AssemblyName System.Windows.Forms
$sig = @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
'@
$U = Add-Type -MemberDefinition $sig -Name U -Namespace W -PassThru
$LEFTDOWN=0x02; $LEFTUP=0x04; $RIGHTDOWN=0x08; $RIGHTUP=0x10; $WHEEL=0x0800

function Move-To($px,$py) { if ($px -ge 0 -and $py -ge 0) { [void]$U::SetCursorPos($px,$py) } }
function Click-Left  { $U::mouse_event($LEFTDOWN,0,0,0,0);  $U::mouse_event($LEFTUP,0,0,0,0) }
function Click-Right { $U::mouse_event($RIGHTDOWN,0,0,0,0); $U::mouse_event($RIGHTUP,0,0,0,0) }

switch ($Action) {
  "move"         { Move-To $X $Y }
  "left_click"   { Move-To $X $Y; Click-Left }
  "right_click"  { Move-To $X $Y; Click-Right }
  "double_click" { Move-To $X $Y; Click-Left; Start-Sleep -Milliseconds 60; Click-Left }
  "type"         { [System.Windows.Forms.SendKeys]::SendWait($Text) }
  "key"          { [System.Windows.Forms.SendKeys]::SendWait($Key) }
  "scroll"       { Move-To $X $Y; $U::mouse_event($WHEEL,0,0,[uint32]($Amount*120),0) }
  default        { Write-Error "unknown action: $Action"; exit 2 }
}
Write-Output "ok"
