@echo off
REM start.bat — Windows wrapper for scripts/start.sh
REM
REM Same behavior: sources .env.local (gitignored), then starts uvicorn.
REM Required because hermes runs commands via cmd.exe, which can't run
REM bash directly. Mirrors scripts/start.sh exactly.
REM
REM Usage:
REM   scripts\start.bat           REM backend (MODE=backend) on :18080
REM   scripts\start.bat mcp       REM MCP server (MODE=mcp) on :18080

setlocal enabledelayedexpansion
set HERE=%~dp0
set ROOT=%HERE%..
cd /d "%ROOT%\retell-quo-server"

REM Load .env.local (KEY=VALUE per line, no expansion needed)
if exist "%ROOT%\.env.local" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ROOT%\.env.local") do (
    set "line=%%A"
    if not "!line:~0,1!"=="#" set %%A=%%B
  )
)

set MODE=%1
if "%MODE%"=="" set MODE=backend
set PORT=%PORT%
if "%PORT%"=="" set PORT=18080
set PYTHONPATH=.

REM Strip dummy placeholders that QuoClient rejects
if "%QUO_API_KEY%"=="dummy" set QUO_API_KEY=
if "%RETELL_API_KEY%"=="dummy" set RETELL_API_KEY=

echo [start.bat] MODE=%MODE% PORT=%PORT%
if defined QUO_API_KEY echo [start.bat] QUO_API_KEY=set
if defined RETELL_API_KEY echo [start.bat] RETELL_API_KEY=set
if defined OPENPHONE_API_KEY echo [start.bat] OPENPHONE_API_KEY=set
if defined COMPOSIO_API_KEY echo [start.bat] COMPOSIO_API_KEY=set

if "%MODE%"=="mcp" (
  python -m src.mcp_main --host 0.0.0.0 --port %PORT%
) else (
  uvicorn src.server:app --host 0.0.0.0 --port %PORT% --proxy-headers --forwarded-allow-ips=*
)