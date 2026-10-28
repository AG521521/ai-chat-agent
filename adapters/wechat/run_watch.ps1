# Always-on watcher: polls the B-account WeChat window, records new-message events.
# Registered as a scheduled task that starts at logon; safe to run in the background.
$ErrorActionPreference = 'Continue'
$root = 'D:\weixin_agent'
$log = Join-Path $root 'logs\watch_runner.log'
$py = 'C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

function Log($msg) { "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg | Out-File -FilePath $log -Append -Encoding utf8 }

# keep a single watcher alive
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*wechat_digest.py*--watch*' }
if ($existing) {
    Log ("watcher already running, pid=" + ($existing.ProcessId -join ','))
    exit 0
}

Log "starting watcher"
$env:PYTHONIOENCODING = 'utf-8'
& $py (Join-Path $root 'wechat_digest.py') --watch 60 *>> $log
Log "watcher exited with $LASTEXITCODE"
