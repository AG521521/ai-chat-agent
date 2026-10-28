@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 抖音视频自动回复助手 ===
echo.
node run.mjs
echo.
pause
