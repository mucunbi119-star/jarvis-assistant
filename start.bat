@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto failed
rem Keep this file ASCII with CRLF for Windows cmd.exe and ZIP downloads.
if not exist ".venv\Scripts\python.exe" goto prepare
".venv\Scripts\python.exe" -c "import sys, fastapi, uvicorn, httpx, multipart, dotenv, pypdf, openpyxl; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 goto run

:prepare
echo First-time setup or environment repair is needed. Internet access is required.
call "%~dp0setup.bat"
if errorlevel 1 exit /b 1

:run
".venv\Scripts\python.exe" scripts\run.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Could not start the assistant. Check the error above and README.md.
pause
exit /b 1
