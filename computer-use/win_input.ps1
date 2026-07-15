# win_input.ps1 — inject one computer-use action into the live Windows session.
# Runs as the interactive OperatorInputBroker scheduled task. The WSL backend
# writes JSON requests into a user-private queue; keeping this process rooted in
# an InteractiveToken session is what gives it real input-desktop access.
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
  [string]$Action = "",
  [int]$X = -1, [int]$Y = -1,
  [string]$Text = "", [string]$Key = "", [int]$Amount = 3,
  [string]$BrokerDir = ""
)
$ErrorActionPreference = "Stop"
$sig = @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@
$U = Add-Type -MemberDefinition $sig -Name U -Namespace W -PassThru
$executor = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace W {
  public static class InputExecutor {
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int X; public int Y; }
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr OpenInputDesktop(uint f, bool i, uint a);
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetThreadDesktop(IntPtr d);
    [DllImport("user32.dll", SetLastError=true)] static extern bool CloseDesktop(IntPtr d);
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll", SetLastError=true)] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern void mouse_event(uint f, uint x, uint y, uint data, int extra);
    [DllImport("user32.dll")] static extern void keybd_event(byte vk, byte scan, uint flags, int extra);

    const uint GENERIC_ALL=0x10000000, LEFTDOWN=0x02, LEFTUP=0x04,
      RIGHTDOWN=0x08, RIGHTUP=0x10, WHEEL=0x0800;
    static readonly Dictionary<string, byte> Keys = new Dictionary<string, byte>(StringComparer.OrdinalIgnoreCase) {
      {"ctrl",0x11},{"control",0x11},{"alt",0x12},{"shift",0x10},
      {"win",0x5B},{"windows",0x5B},{"super",0x5B},{"meta",0x5B},{"cmd",0x5B},
      {"left",0x25},{"up",0x26},{"right",0x27},{"down",0x28},
      {"enter",0x0D},{"return",0x0D},{"esc",0x1B},{"escape",0x1B},{"tab",0x09},
      {"space",0x20},{"home",0x24},{"end",0x23},{"page_up",0x21},{"pgup",0x21},
      {"page_down",0x22},{"pgdn",0x22},{"delete",0x2E},{"backspace",0x08}
    };

    static byte KeyCode(string name) {
      name=name.Trim(); byte value;
      if (Keys.TryGetValue(name, out value)) return value;
      if (name.Length==1) return (byte)Char.ToUpperInvariant(name[0]);
      int number;
      if (name.Length>1 && (name[0]=='f' || name[0]=='F') &&
          Int32.TryParse(name.Substring(1), out number) && number>=1 && number<=24)
        return (byte)(0x6F+number);
      throw new ArgumentException("unknown key: "+name);
    }
    static bool Extended(byte key) {
      return key==0x25 || key==0x26 || key==0x27 || key==0x28 || key==0x24 ||
             key==0x23 || key==0x21 || key==0x22 || key==0x2D || key==0x2E;
    }
    static void Move(int x,int y) {
      if (x>=0 && y>=0 && !SetCursorPos(x,y))
        throw new Win32Exception(Marshal.GetLastWin32Error(), "SetCursorPos rejected input");
    }
    static void Hotkey(string combo) {
      string[] names=combo.Split('+'); byte[] codes=new byte[names.Length];
      for (int i=0;i<names.Length;i++) { codes[i]=KeyCode(names[i]); keybd_event(codes[i],0,Extended(codes[i])?1u:0u,0); Thread.Sleep(15); }
      for (int i=codes.Length-1;i>=0;i--) { keybd_event(codes[i],0,Extended(codes[i])?3u:2u,0); Thread.Sleep(15); }
    }
    static void Execute(string kind,int x,int y,string text,string key,int amount) {
      if (kind=="probe") { POINT p; if (!GetCursorPos(out p)) throw new Win32Exception(); Move(p.X,p.Y); }
      else if (kind=="move") Move(x,y);
      else if (kind=="left_click") { Move(x,y); mouse_event(LEFTDOWN,0,0,0,0); mouse_event(LEFTUP,0,0,0,0); }
      else if (kind=="right_click") { Move(x,y); mouse_event(RIGHTDOWN,0,0,0,0); mouse_event(RIGHTUP,0,0,0,0); }
      else if (kind=="double_click") { Move(x,y); mouse_event(LEFTDOWN,0,0,0,0); mouse_event(LEFTUP,0,0,0,0); Thread.Sleep(60); mouse_event(LEFTDOWN,0,0,0,0); mouse_event(LEFTUP,0,0,0,0); }
      else if (kind=="type") SendKeys.SendWait(text ?? "");
      else if (kind=="key") SendKeys.SendWait(key ?? "");
      else if (kind=="hotkey") Hotkey(key ?? "");
      else if (kind=="scroll") { Move(x,y); mouse_event(WHEEL,0,0,unchecked((uint)(amount*120)),0); }
      else throw new ArgumentException("unknown action: "+kind);
    }
    public static void Invoke(string kind,int x,int y,string text,string key,int amount) {
      Exception failure=null; IntPtr desktop=IntPtr.Zero;
      Thread thread=new Thread(delegate() {
        try {
          desktop=OpenInputDesktop(0,false,GENERIC_ALL);
          if (desktop==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(),"OpenInputDesktop failed");
          if (!SetThreadDesktop(desktop)) throw new Win32Exception(Marshal.GetLastWin32Error(),"SetThreadDesktop failed");
          Execute(kind,x,y,text,key,amount);
        } catch (Exception e) { failure=e; }
      });
      thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
      if (desktop!=IntPtr.Zero) CloseDesktop(desktop);
      if (failure!=null) throw new Exception(failure.Message,failure);
    }
  }
}
'@
Add-Type -TypeDefinition $executor -ReferencedAssemblies System.Windows.Forms
# DPI AWARENESS — MUST match win_capture.ps1, which calls SetProcessDPIAware()
# before reading Screen.Bounds and so measures the desktop in PHYSICAL pixels
# (e.g. 2647x1664 at 150% scale). win_backend derives its click scale factors
# from that physical size. Without this call, THIS process is DPI-UNAWARE, so
# SetCursorPos interprets its args as LOGICAL (DPI-scaled, 1765x1109) coords and
# Windows re-virtualizes them — every click landed ~1.5x off-target (owner
# 2026-07-12: "clicks/keys aren't landing on the desktop"). Making both scripts
# DPI-aware puts capture and injection in the SAME physical-pixel space.
[void]$U::SetProcessDPIAware()

function Invoke-Action([string]$kind, [int]$px, [int]$py,
                       [string]$text, [string]$key, [int]$amount) {
  [W.InputExecutor]::Invoke($kind, $px, $py, $text, $key, $amount)
}

function Write-JsonAtomic([string]$path, $value) {
  $tmp = "$path.$PID.tmp"
  $value | ConvertTo-Json -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $path -Force
}

if ($BrokerDir) {
  New-Item -ItemType Directory -Path $BrokerDir -Force | Out-Null
  $mutex = New-Object Threading.Mutex($false, "Local\OperatorInputBroker")
  try {
    $acquired = $mutex.WaitOne(0)
  } catch [Threading.AbandonedMutexException] {
    # The prior broker died without releasing the mutex; this process owns it.
    $acquired = $true
  }
  if (-not $acquired) { throw "OperatorInputBroker is already running" }
  try {
    # Heartbeat means "can inject", not merely "a PowerShell loop exists".
    # The same call returns false from a WSL/detached window station.
    Invoke-Action "probe" -1 -1 "" "" 0
    while ($true) {
      Write-JsonAtomic (Join-Path $BrokerDir "heartbeat.json") `
        @{ ok = $true; pid = $PID; ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
      $requests = @(Get-ChildItem -LiteralPath $BrokerDir -Filter "*.request.json" `
                    -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc)
      foreach ($requestFile in $requests) {
        $responsePath = $requestFile.FullName -replace '\.request\.json$', '.response.json'
        try {
          $request = Get-Content -LiteralPath $requestFile.FullName -Raw | ConvertFrom-Json
          Invoke-Action ([string]$request.action) ([int]$request.x) ([int]$request.y) `
                        ([string]$request.text) ([string]$request.key) ([int]$request.amount)
          Write-JsonAtomic $responsePath @{ ok = $true }
        } catch {
          Write-JsonAtomic $responsePath @{ ok = $false; error = $_.Exception.Message }
        } finally {
          Remove-Item -LiteralPath $requestFile.FullName -Force -ErrorAction SilentlyContinue
        }
      }
      Start-Sleep -Milliseconds 50
    }
  } finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
  }
}

if (-not $Action) { throw "-Action or -BrokerDir is required" }
Invoke-Action $Action $X $Y $Text $Key $Amount
Write-Output "ok"
