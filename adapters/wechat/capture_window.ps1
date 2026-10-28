param(
    [Parameter(Mandatory = $true)][long]$Hwnd,
    [Parameter(Mandatory = $true)][string]$OutDir
)

# 按窗口句柄截图（DPI 感知），输出 <OutDir>\cap.png
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class CapOne {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
}
"@
[void][CapOne]::SetProcessDPIAware()

$r = New-Object 'CapOne+RECT'
[void][CapOne]::GetWindowRect([IntPtr]$Hwnd, [ref]$r)
$w = $r.R - $r.L
$h = $r.B - $r.T
if ($w -le 0 -or $h -le 0) { throw "bad window rect" }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$png = Join-Path $OutDir 'cap.png'

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[void][CapOne]::PrintWindow([IntPtr]$Hwnd, $hdc, 2)
$g.ReleaseHdc($hdc)
$g.Dispose()
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host "captured ${w}x${h} -> $png"
