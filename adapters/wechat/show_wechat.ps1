# Bring the hidden WeChat window back on screen (clears the alpha-0 / tool-window styles).
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class ShowW {
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
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
    [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte alpha, uint flags);
    [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte scan, uint flags, UIntPtr extra);
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
            else if (w > 900 && hh > 700) res.Add(h);
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@
[void][ShowW]::SetProcessDPIAware()

$stateFile = 'D:\weixin_agent\window_state.txt'
Set-Content -Path $stateFile -Value 'shown' -Encoding utf8

$targets = @([ShowW]::ChatWindows())
if (-not $targets.Count) { 'no WeChat chat window found'; exit 3 }

$screenW = [ShowW]::GetSystemMetrics(0)
$screenH = [ShowW]::GetSystemMetrics(1)
$w = 2100; $ht = 1330
$x = [int](($screenW - $w) / 2)
$y = [int](($screenH - $ht) / 2)

foreach ($h in $targets) {
    $style = [ShowW]::GetWindowLong($h, -20)
    $style = $style -band (-bnot 0x00080000) -band (-bnot 0x00000080)   # drop LAYERED | TOOLWINDOW
    [void][ShowW]::SetWindowLong($h, -20, $style)
    [void][ShowW]::SetLayeredWindowAttributes($h, 0, 255, 2)
    [void][ShowW]::ShowWindow($h, 9)
    Start-Sleep -Milliseconds 400
    [void][ShowW]::SetWindowPos($h, [IntPtr]::Zero, $x, $y, $w, $ht, 0x0040)
    Start-Sleep -Milliseconds 300
    [ShowW]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 50
    [ShowW]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    [void][ShowW]::SetForegroundWindow($h)
    "window shown at $x,$y size ${w}x${ht}"
}
