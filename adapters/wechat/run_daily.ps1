# Daily WeChat digest runner (called by Windows Task Scheduler).
# Captures the always-on B account window, OCRs the session list, writes
# daily/YYYY-MM-DD_HHMM.md + digest json + screenshot, and refreshes daily/latest.md.
$ErrorActionPreference = 'Continue'
$root = 'D:\weixin_agent'
$log = Join-Path $root 'logs\daily.log'
$py = 'C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

function Log($msg) { "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg | Out-File -FilePath $log -Append -Encoding utf8 }

Log "=== run start ==="

$env:PYTHONIOENCODING = 'utf-8'
$out = & $py (Join-Path $root 'wechat_digest.py') --auto --tag daily --md --include-events 2>&1
$out | Out-File -FilePath $log -Append -Encoding utf8
$code = $LASTEXITCODE
Log "exit=$code"

$latest = Get-ChildItem (Join-Path $root 'daily') -Filter '*.md' |
    Where-Object { $_.Name -ne 'latest.md' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    Copy-Item $latest.FullName (Join-Path $root 'daily\latest.md') -Force
    Log "latest -> $($latest.Name)"
}
Log "=== run end ==="
