@echo off
chcp 65001 >nul
set "PROFILE=%~dp0chrome-profile"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo [ERROR] Chrome not found at %CHROME%
  exit /b 1
)

echo [1/2] Killing only the automation Chrome instance (leaves your daily Chrome alone)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*douyin-agent*chrome-profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo [2/2] Starting automation Chrome on port 9222, profile: %PROFILE%
start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-blink-features=AutomationControlled ^
  --window-size=1500,1000 ^
  "https://www.douyin.com/"

echo.
echo Chrome started. Log in by scanning the QR code, then run: npm run probe
