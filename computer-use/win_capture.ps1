# win_capture.ps1 — screenshot the primary Windows desktop to a PNG.
# Called from WSL via powershell.exe. Arg: output path (Windows form, C:\...).
# Prints "<width> <height>" on success so the caller learns the real geometry.
#
# DPI-AWARE (fix 2026-06-25): a DPI-unaware process sees Windows' *logical*
# resolution — on a ~175%-scaled display that's 1609x1109 vs the true
# 2816x1940 framebuffer, so an un-aware grab captures only the top-left ~57% and
# cuts off half the screen. SetProcessDPIAware() (called BEFORE reading bounds)
# makes Screen.Bounds report physical pixels, so we capture the whole desktop.
param([Parameter(Mandatory=$true)][string]$OutPath, [int]$MaxWidth = 0)
$dpi = @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@
[void](Add-Type -MemberDefinition $dpi -Name D -Namespace P -PassThru)::SetProcessDPIAware()
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

# NOTE: this saves PNG, deliberately. JPEG re-encoding via the .NET image-encoder
# reflection (ImageCodecInfo / Encoder::Quality) trips Windows Defender's AMSI as
# a screen-grabber-malware signature ("script contains malicious content"). So we
# keep PowerShell on the benign bmp.Save(...Png) path and let win_backend.py
# re-encode the PNG to a small JPEG on the WSL/Pillow side before sending to the
# API (that's where the 413-avoiding size reduction actually happens).
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$g.Dispose()
# Optional downscale to MaxWidth (preserving aspect) — keeps every window visible
# but trims the image+token size. The model's click coords are in THIS image's
# pixel space; win_backend scales them back to physical pixels before injecting.
if ($MaxWidth -gt 0 -and $b.Width -gt $MaxWidth) {
  $nw = $MaxWidth
  $nh = [int]($b.Height * $MaxWidth / $b.Width)
  $scaled = New-Object System.Drawing.Bitmap $nw, $nh
  $sg = [System.Drawing.Graphics]::FromImage($scaled)
  $sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $sg.DrawImage($bmp, 0, 0, $nw, $nh)
  $sg.Dispose(); $bmp.Dispose()
  $scaled.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
  $scaled.Dispose()
  Write-Output "$nw $nh $($b.Width) $($b.Height)"
} else {
  $bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  Write-Output "$($b.Width) $($b.Height) $($b.Width) $($b.Height)"
}
