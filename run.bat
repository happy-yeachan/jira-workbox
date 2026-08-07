@echo off
REM Windows launcher: double-click to install uv (if needed), then run the server.
REM uv fetches the right Python and all dependencies from uv.lock on first run.
setlocal
cd /d "%~dp0"

if "%WORKBOX_HOST%"=="" set WORKBOX_HOST=127.0.0.1
if "%WORKBOX_PORT%"=="" set WORKBOX_PORT=8000

where uv >nul 2>nul
if errorlevel 1 (
  echo uv가 없어 자동 설치를 시도합니다... ^(astral.sh 공식 설치 스크립트^)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  REM uv installs to %USERPROFILE%\.local\bin; add it to PATH for this session
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo uv 자동 설치에 실패했습니다. 아래를 직접 실행한 뒤 이 파일을 다시 실행하세요:
  echo    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  pause
  exit /b 1
)

REM Credentials live in Windows Credential Manager, not in this folder. If none
REM are stored yet, the server still starts and the page shows a setup form.
echo 의존성 준비 중... (처음 한 번은 파이썬·패키지를 받느라 조금 걸립니다)

start "" "http://%WORKBOX_HOST%:%WORKBOX_PORT%/"
uv run uvicorn app:app --host %WORKBOX_HOST% --port %WORKBOX_PORT%
