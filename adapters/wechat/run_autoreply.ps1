$ErrorActionPreference='Continue'
$root='D:\weixin_agent'
$log=Join-Path $root 'logs\autoreply_runner.log'
$out = & 'C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe' (Join-Path $root 'src\autoreply_worker.py') 2>&1
"[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), ($out -join ' ') | Out-File -FilePath $log -Append -Encoding utf8