# F stage auto runner wrapper (ASCII only on purpose: Windows PowerShell 5.1
# reads .ps1 as ANSI and would mangle Chinese arguments).
$ErrorActionPreference = 'Continue'
$root = 'D:\weixin_agent'
$log = Join-Path $root 'logs\auto_agent.log'
$py = 'C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

# single instance guard
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*auto_runner.py*' }
if ($existing) {
    "[{0}] already running pid={1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), ($existing.ProcessId -join ',') |
        Out-File -FilePath $log -Append -Encoding utf8
    exit 0
}

$env:PYTHONIOENCODING = 'utf-8'
# contacts are read from config/auto_contacts.json (UTF-8), not from argv
$out = & $py (Join-Path $root 'src\auto_runner.py') --once --send --live 2>&1
"[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), ($out -join ' ') |
    Out-File -FilePath $log -Append -Encoding utf8
