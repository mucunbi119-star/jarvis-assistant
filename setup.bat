@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto failed
rem Keep this file ASCII with CRLF: GitHub ZIPs must also work in cmd.exe.
rem Detect existing Python runtimes without asking the new manager to install one.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "JARVIS_PYTHON_CMD="
echo Preparing the Japanese assistant...
if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 goto install

:find_python
call :probe py -3
for %%V in (3.14 3.13 3.12 3.11) do call :probe py -%%V
call :probe python
call :probe python3
if not defined JARVIS_PYTHON_CMD goto python_missing
%JARVIS_PYTHON_CMD% -m venv .venv
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" goto failed

:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Setup completed. Press any key to continue.
pause
exit /b 0

:python_missing
echo.
echo Python 3.11 or newer could not be started.
echo It may be missing, too old, or unavailable through PATH / the Python launcher.
echo.
echo 1. Open https://www.python.org/downloads/windows/
echo 2. Install a supported 64-bit Python runtime, for example Python 3.13.
echo    With the classic installer, select "Add python.exe to PATH".
echo    With Python install manager, install a runtime as well as the manager.
echo 3. Close this window, then double-click start.bat again.
echo Opening the Japanese setup guide in your browser.
if exist "%~dp0PYTHON_SETUP.html" start "" "%~dp0PYTHON_SETUP.html"
echo See README.md for Japanese instructions.
pause
exit /b 1

:failed
echo.
echo Setup failed. Please check the error above and README.md.
echo No application was started. Close this window before retrying.
pause
exit /b 1

:probe
if defined JARVIS_PYTHON_CMD exit /b 0
%* -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "JARVIS_PYTHON_CMD=%*"
exit /b 0
