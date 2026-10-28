@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 白名单 定时自动回复 ===
node loop.mjs
pause
