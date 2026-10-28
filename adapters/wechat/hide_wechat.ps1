param(
    [int]$Width = 2100,
    [int]$Height = 1330
)

# Make the WeChat chat window completely invisible (alpha 0) but still "shown",
# so PrintWindow keeps returning real pixels while nothing appears on screen.
# Fullscreen/exclusive apps are unaffected; the window also leaves taskbar and alt-tab.
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$ErrorActionPreference = 'Continue'

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class HideW {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
    [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte alpha, uint flags);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int idx);

    public static List<IntPtr> ChatWindows() {
        var res = new List<IntPtr>();
        EnumWindows((h, l) => {
            var c = new StringBuilder(256); GetClassNameW(h, c, 256);
            if (c.ToString() != "Qt51514QWindowIcon") return true;
            uint pid; GetWindowThreadProcessId(h, out pid);
            try { if (System.Diagnostics.Process.GetProcessById((int)pid).ProcessName != "Weixin") return true; }
            catch { return true; }
            RECT r; GetWindowRect(h, out r);
            int w = r.R - r.L, hh = r.B - r.T;
            if (IsIconic(h)) { if (w > 80 && hh > 20) res.Add(h); }
            else if (IsWindowVisible(h) && w > 900 && hh > 700) res.Add(h);
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@
[void][HideW]::SetProcessDPIAware()

$stateFile = 'D:\weixin_agent\window_state.txt'
Set-Content -Path $stateFile -Value 'hidden' -Encoding utf8

$targets = @([HideW]::ChatWindows())
if (-not $targets.Count) { 'no WeChat chat window found'; exit 3 }

$screenW = [HideW]::GetSystemMetrics(0)
$screenH = [HideW]::GetSystemMetrics(1)
$cx = [int](($screenW - $Width) / 2)
$cy = [int](($screenH - $Height) / 2)

foreach ($h in $targets) {
    if ([HideW]::IsIconic($h)) { [void][HideW]::ShowWindow($h, 9); Start-Sleep -Milliseconds 600 }
    [void][HideW]::SetWindowPos($h, [IntPtr]::Zero, $cx, $cy, $Width, $Height, 0x0014)
    $style = [HideW]::GetWindowLong($h, -20)
    [void][HideW]::SetWindowLong($h, -20, $style -bor 0x00080000 -bor 0x00000080)   # LAYERED | TOOLWINDOW
    [void][HideW]::SetLayeredWindowAttributes($h, 0, 0, 2)                            # alpha = 0
    "hwnd=$h hidden at $cx,$cy size ${Width}x${Height}"
}
