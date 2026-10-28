@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 初回の準備を行います。インターネット接続が必要です。
  call setup.bat
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" scripts\run.py
if errorlevel 1 (
  echo.
  echo 起動できませんでした。上のエラーと README を確認してください。
  pause
  exit /b 1
)
