@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 抖音自动回复（全部类型） ===
if not exist config.json (echo [缺少 config.json] & pause & exit /b 1)
node auto-all.mjs
echo.
echo 跑完，结果看 sent.json
pause
