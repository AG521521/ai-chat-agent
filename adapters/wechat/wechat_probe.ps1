param(
    [int]$ProcId = 0,
    [string]$OutDir = 'D:\weixin_agent\logs',
    [int]$MaxDepth = 14,
    [int]$Width = 0,
    [int]$Height = 0,
    [switch]$AllowRestore,
    [switch]$FlashRestore,
    [switch]$EnsureHidden,
    [switch]$UiTree
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class CapW {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
    [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte alpha, uint flags);

    // chat window = visible Qt window; a minimized one is accepted so we can restore it
    public static List<IntPtr> ChatWindows(uint pid) {
        var res = new List<IntPtr>();
        EnumWindows((h, l) => {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != pid) return true;
            var c = new StringBuilder(256); GetClassNameW(h, c, 256);
            if (c.ToString() != "Qt51514QWindowIcon") return true;
            RECT r; GetWindowRect(h, out r);
            int w = r.Right - r.Left, hh = r.Bottom - r.Top;
            bool iconic = IsIconic(h);
            if (!iconic && !IsWindowVisible(h)) return true;
            if (iconic) {
                if (w < 80 || hh < 20) return true;   // tray/ghost stubs
                res.Add(h);
                return true;
            }
            if (w < 900 || hh < 700) return true;     // dialogs/popups
            res.Add(h);
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@

# capture must run DPI-aware, otherwise PrintWindow returns a cropped logical-size bitmap
try { [void][CapW]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch { [void][CapW]::SetProcessDPIAware() }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

# pick the real chat window: largest visible Qt window per process
function Get-TargetWindows {
    Get-Process -Name Weixin -ErrorAction SilentlyContinue | ForEach-Object {
        $cands = @([CapW]::ChatWindows([uint32]$_.Id))
        if (-not $cands.Count) { return }
        $best = $null; $bestArea = 0
        foreach ($h in $cands) {
            $r = New-Object 'CapW+RECT'
            if (-not [CapW]::GetWindowRect($h, [ref]$r)) { continue }
            $area = ($r.Right - $r.Left) * ($r.Bottom - $r.Top)
            if ($area -gt $bestArea) { $bestArea = $area; $best = $h }
        }
        if ($best) {
            $sb = New-Object System.Text.StringBuilder 256
            [void][CapW]::GetWindowTextW($best, $sb, 256)
            [pscustomobject]@{ Pid = $_.Id; Handle = $best; Title = $sb.ToString() }
        }
    }
}

$targets = Get-TargetWindows
if ($ProcId -ne 0) { $targets = $targets | Where-Object { $_.Pid -eq $ProcId } }
if (-not $targets) { throw "no visible Weixin main window found" }

$stateFile = 'D:\weixin_agent\window_state.txt'
$userWantsVisible = (Test-Path $stateFile) -and ((Get-Content $stateFile -Raw -ErrorAction SilentlyContinue).Trim() -eq 'shown')

foreach ($t in $targets) {
    $wasMin = [CapW]::IsIconic($t.Handle)
    if ($EnsureHidden -and -not $userWantsVisible) {
        # alpha 0 + tool window: invisible to the user, still rendered for PrintWindow
        $style = [CapW]::GetWindowLong($t.Handle, -20)
        [void][CapW]::SetWindowLong($t.Handle, -20, $style -bor 0x00080000 -bor 0x00000080)
        [void][CapW]::SetLayeredWindowAttributes($t.Handle, 0, 0, 2)
    }
    if ($wasMin) {
        if (-not $AllowRestore) {
            # background polling must never yank a minimized window onto the screen
            Write-Host "pid=$($t.Pid) minimized, skipped (no -AllowRestore)"
            continue
        }
        # restore once and keep it restored: re-minimising every poll would flash on screen
        [void][CapW]::ShowWindow($t.Handle, 9)
        Start-Sleep -Milliseconds 700
        Write-Host "pid=$($t.Pid) restored from minimized state"
    }
    if ($Width -gt 400 -and $Height -gt 400) {
        $cur = New-Object 'CapW+RECT'
        [void][CapW]::GetWindowRect($t.Handle, [ref]$cur)
        $dw = [Math]::Abs(($cur.Right - $cur.Left) - $Width)
        $dh = [Math]::Abs(($cur.Bottom - $cur.Top) - $Height)
        if ($dw -gt 40 -or $dh -gt 40) {
            # keep the window position, only normalise the size so list geometry stays predictable
            [void][CapW]::SetWindowPos($t.Handle, [IntPtr]::Zero, 0, 0, $Width, $Height, 0x0016)
            Start-Sleep -Milliseconds 700
        }
    }
    $rect = New-Object 'CapW+RECT'
    if (-not [CapW]::GetWindowRect($t.Handle, [ref]$rect)) { Write-Host "pid=$($t.Pid) GetWindowRect failed"; continue }
    $w = $rect.Right - $rect.Left
    $h = $rect.Bottom - $rect.Top
    if ($w -lt 200 -or $h -lt 200) { Write-Host "pid=$($t.Pid) window too small (${w}x${h}), skipped"; continue }

    $png = Join-Path $OutDir ("shot_pid{0}.png" -f $t.Pid)
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    $ok = [CapW]::PrintWindow($t.Handle, $hdc, 2)
    $g.ReleaseHdc($hdc)
    $g.Dispose()
    $bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host ("pid={0} title='{1}' size={2}x{3} printwindow={4} -> {5}" -f $t.Pid, $t.Title, $w, $h, $ok, $png)
    if ($wasMin -and $FlashRestore) {
        # put it back the way we found it so the capture does not leave a window on screen
        [void][CapW]::ShowWindow($t.Handle, 6)
        Write-Host "pid=$($t.Pid) minimized again"
    }
}

if ($UiTree) {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
    $sb = New-Object System.Text.StringBuilder
    function Line($ci, $depth) {
        return ('{0}{1}  name="{2}"  cls="{3}"  auto="{4}"  off={5}' -f ('  ' * $depth), $ci.ControlType.ProgrammaticName.Replace('ControlType.', ''), $ci.Name, $ci.ClassName, $ci.AutomationId, $ci.IsOffscreen)
    }
    function WalkChildren($parent, $depth) {
        if ($depth -gt $MaxDepth -or $parent -eq $null) { return }
        $cur = $null
        try { $cur = $walker.GetFirstChild($parent) } catch { return }
        $count = 0
        while ($cur -ne $null -and $count -lt 300) {
            try {
                $ci = $cur.Current
                [void]$sb.AppendLine((Line $ci $depth))
                WalkChildren $cur ($depth + 1)
            } catch { }
            try { $cur = $walker.GetNextSibling($cur) } catch { break }
            $count++
        }
    }
    function Walk($el, $depth) {
        try { [void]$sb.AppendLine((Line $el.Current $depth)) } catch { }
        WalkChildren $el ($depth + 1)
    }
    foreach ($t in $targets) {
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($t.Handle)
            [void]$sb.AppendLine("=== pid=$($t.Pid) title='$($t.Title)' ===")
            Walk $root 0
        } catch { [void]$sb.AppendLine("pid=$($t.Pid) UIA failed: $($_.Exception.Message)") }
    }
    $uiFile = Join-Path $OutDir 'uitree.txt'
    $sb.ToString() | Out-File -FilePath $uiFile -Encoding utf8
    Write-Host "uitree -> $uiFile"
}
