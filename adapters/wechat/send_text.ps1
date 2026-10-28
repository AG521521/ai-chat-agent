param(
    [Parameter(Mandatory = $true)][string]$Chat,
    [Parameter(Mandatory = $true)][string]$Text,
    [switch]$Send,                      # 不传就是 dry-run：只把字填进输入框，不按回车
    [string[]]$Allowed = @('小A'),
    [int]$WaitMs = 1200
)

# 在白名单会话里发一条文本。先露脸 -> 搜索打开会话 -> OCR 核对标题 -> 粘贴 -> （可选）回车 -> 重新隐身。
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$root = 'D:\weixin_agent'
$logFile = Join-Path $root 'logs\send.log'
function Log($m) { "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m | Out-File -FilePath $logFile -Append -Encoding utf8 }

if ($Allowed -notcontains $Chat) { Log "REFUSE: '$Chat' not in whitelist"; exit 10 }
if ([string]::IsNullOrWhiteSpace($Text)) { Log 'REFUSE: empty text'; exit 11 }
if ($Text.Length -gt 120) { Log 'REFUSE: text too long'; exit 12 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class SW {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
    [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte alpha, uint flags);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int idx);

    public static List<IntPtr> WeChatWindows() {
        var res = new List<IntPtr>();
        EnumWindows((h, l) => {
            var c = new StringBuilder(256); GetClassNameW(h, c, 256);
            if (c.ToString() != "Qt51514QWindowIcon") return true;
            uint pid; GetWindowThreadProcessId(h, out pid);
            try { if (System.Diagnostics.Process.GetProcessById((int)pid).ProcessName != "Weixin") return true; }
            catch { return true; }
            RECT r; GetWindowRect(h, out r);
            int w = r.R - r.L, hh = r.B - r.T;
            if (w > 900 && hh > 700) res.Add(h);
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@
[void][SW]::SetProcessDPIAware()

$hwnds = @([SW]::WeChatWindows())
if (-not $hwnds.Count) { Log 'no WeChat chat window'; exit 20 }
$hwnd = $hwnds[0]

function Get-Rect([IntPtr]$h) {
    $r = New-Object 'SW+RECT'
    [void][SW]::GetWindowRect($h, [ref]$r)
    return $r
}

# 1) 露脸（脱掉 alpha=0 / toolwindow，居中到 2100x1330）
$st = [SW]::GetWindowLong($hwnd, -20)
[void][SW]::SetWindowLong($hwnd, -20, $st -band (-bnot 0x00080000) -band (-bnot 0x00000080))
[void][SW]::SetLayeredWindowAttributes($hwnd, 0, 255, 2)
[void][SW]::ShowWindow($hwnd, 9)
Set-Content -Path (Join-Path $root 'window_state.txt') -Value 'shown' -Encoding utf8
$sw = [SW]::GetSystemMetrics(0); $sh = [SW]::GetSystemMetrics(1)
[void][SW]::SetWindowPos($hwnd, [IntPtr]::Zero, [int](($sw - 2100) / 2), [int](($sh - 1330) / 2), 2100, 1330, 0x0040)
Start-Sleep -Milliseconds 400
$rect = Get-Rect $hwnd
Log ("window shown at {0},{1} {2}x{3}" -f $rect.L, $rect.T, ($rect.R - $rect.L), ($rect.B - $rect.T))

# 前台 + 焦点
[SW]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 50
[SW]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero); Start-Sleep -Milliseconds 100
[void][SW]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 400
if ([SW]::GetForegroundWindow() -ne $hwnd) { Log 'WARN: window not foreground; aborting'; exit 21 }

# 2) 点搜索框（UIA 找名叫“搜索”的元素，退化为固定坐标）
$searchBox = $null
try {
    $rootEl = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, '搜索')
    $el = $rootEl.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if ($el) {
        $br = $el.Current.BoundingRectangle
        if ($br.Width -gt 20 -and $br.Height -gt 10) { $searchBox = $br }
    }
} catch { }
if (-not $searchBox) {
    # 会话列表顶部搜索框的固定位置（2100x1330 布局）
    $searchBox = [pscustomobject]@{ X = $rect.L + 150; Y = $rect.T + 95; Width = 260; Height = 40 }
    Log 'search box: fallback coordinates'
}
$sx = [int]($searchBox.X + $searchBox.Width / 2)
$sy = [int]($searchBox.Y + $searchBox.Height / 2)
Log "click search box at $sx,$sy"
[void][SW]::SetCursorPos($sx, $sy); Start-Sleep -Milliseconds 200
[SW]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); [SW]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 500

# 3) 粘贴会话名（中文走剪贴板）
[System.Windows.Forms.Clipboard]::SetText($Chat)
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('^a')
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 1800
# 回车在微信里会打开“搜一搜”面板；要改成点下拉里的第一条结果
$rx = [int]($rect.L + 300)
$ry = [int]($rect.T + 250)
Log "click first search result at $rx,$ry"
[void][SW]::SetCursorPos($rx, $ry); Start-Sleep -Milliseconds 200
[SW]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
[SW]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 1500
Log "opened chat via search: $Chat"

# 4) 用 OCR 核对标题（聊天区顶部）
function Get-TopText([IntPtr]$h) {
    Add-Type -AssemblyName System.Drawing
    $r = Get-Rect $h
    $w = $r.R - $r.L; $hh = $r.B - $r.T
    $bmp = New-Object System.Drawing.Bitmap($w, 240)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, 240)))
    $g.Dispose()
    $tmp = Join-Path $env:TEMP 'wx_title.png'
    $bmp.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'ocr.ps1') -ImagePath $tmp -Json 2>$null
    return ($out -join ' ')
}

$titleText = Get-TopText $hwnd
$ok = $titleText -like "*$Chat*"
Log ("title check: " + $(if ($ok) { 'OK' } else { 'MISMATCH' }))
if (-not $ok) {
    Log ('abort: header does not contain ' + $Chat)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'hide_wechat.ps1') | Out-Null
    exit 30
}

# 5) 点输入框 + 粘贴正文
$inputX = [int]($rect.L + ($rect.R - $rect.L) * 0.62)
$inputY = [int]($rect.B - 120)
Log "click input at $inputX,$inputY"
[void][SW]::SetCursorPos($inputX, $inputY); Start-Sleep -Milliseconds 200
[SW]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); [SW]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 400
[System.Windows.Forms.Clipboard]::SetText($Text)
Start-Sleep -Milliseconds 150
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 700

# 6) 截图存档（发送前）
Add-Type -AssemblyName System.Drawing
$shot = Join-Path $root ('logs\send_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + $(if ($Send) { '_sent.png' } else { '_staged.png' }))
$bmp2 = New-Object System.Drawing.Bitmap(($rect.R - $rect.L), ($rect.B - $rect.T))
$g2 = [System.Drawing.Graphics]::FromImage($bmp2)
$g2.CopyFromScreen($rect.L, $rect.T, 0, 0, (New-Object System.Drawing.Size(($rect.R - $rect.L), ($rect.B - $rect.T))))
$g2.Dispose(); $bmp2.Save($shot, [System.Drawing.Imaging.ImageFormat]::Png); $bmp2.Dispose()
Log "screenshot: $shot"

if (-not $Send) {
    Log 'dry-run: text staged, Enter NOT pressed'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'hide_wechat.ps1') | Out-Null
    exit 0
}

# 7) 真发
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Milliseconds 900
Log 'send: Enter pressed'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'hide_wechat.ps1') | Out-Null
exit 0
