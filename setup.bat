@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo 日本語アシスタントの準備を始めます。
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
    goto :venv_created
  )
)
where python >nul 2>nul
if errorlevel 1 goto :python_missing
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :python_missing
python -m venv .venv
:venv_created
if not exist ".venv\Scripts\python.exe" goto :failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo 準備ができました。次は start.bat をダブルクリックしてください。
pause
exit /b 0
:python_missing
echo.
echo Python 3.11 以降が必要です。
echo https://www.python.org/downloads/windows/ から Python をインストールしてください。
echo インストール時に「Add python.exe to PATH」を選び、もう一度実行してください。
pause
exit /b 1
:failed
echo.
echo 準備に失敗しました。上のエラーと README の「よくあるエラー」を確認してください。
pause
exit /b 1
