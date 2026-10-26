@echo off
rem ============================================================
rem  data-agent-lite/basic  one-key START (backend :8001 + frontend :5174)
rem  usage: double-click, or  scripts\start_all.bat   (any cwd works)
rem  stop : scripts\stop_all.bat
rem  note : keep this file pure ASCII (cmd reads bat in GBK on zh-CN)
rem ============================================================
setlocal
rem script lives in basic\scripts\ ; uvicorn must run from the repo root
rem (the importable package is "basic.*"), so ROOT = two levels up.
cd /d "%~dp0..\.."
set "ROOT=%CD%"

rem ---- 1) pick a working Python ------------------------------------
rem NB: no set-then-%use% inside one ( ) block - cmd expands %VAR% at parse time.
set "PY="
set "C1=%ROOT%\.venv\Scripts\python.exe"
set "C2=C:\Users\86132\AppData\Local\Programs\Python\Python311\python.exe"
if exist "%C1%" call :trypython "%C1%"
if not defined PY call :trypython "%C2%"
if not defined PY (
    echo [start] no usable Python found.
    echo [start] checked: %C1%  and  %C2%
    echo [start] if "python" works in your shell, edit the C2= line in this script.
    pause
    exit /b 1
)
echo [start] python = %PY%

rem ---- 2) backend :8001 (uvicorn runs from repo root: package is "basic.*") ----
netstat -ano | findstr /r /c:":8001 .*LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo [start] port 8001 already in use - backend seems running, skip.
) else (
    rem redirect MUST live inside cmd /c "..." - a bare "start prog >> log" is
    rem swallowed by the outer shell and the child writes nowhere.
    start "data-agent-backend" /d "%ROOT%" /min cmd /c ""%PY%" -m uvicorn basic.main:app --host 127.0.0.1 --port 8001 >> "basic\uvicorn.log" 2>&1"
    echo [start] backend launching, log: basic\uvicorn.log
)

rem ---- 3) frontend :5174 -------------------------------------------
if not exist "%ROOT%\basic\web\node_modules" (
    echo [start] first run: npm install ...
    pushd "%ROOT%\basic\web"
    call npm install
    popd
)
netstat -ano | findstr /r /c:":5174 .*LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo [start] port 5174 already in use - frontend seems running, skip.
) else (
    start "data-agent-frontend" /d "%ROOT%\basic\web" /min cmd /c "npm run dev >> ..\vite.log 2>&1"
    echo [start] frontend launching, log: basic\vite.log
)

rem ---- 4) wait for backend, then verify + open browser --------------
echo [start] waiting for backend healthz ...
set "READY="
for /l %%i in (1,1,30) do (
    if not defined READY (
        "%PY%" -c "import urllib.request,sys; sys.exit(0 if 'ok' in urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2).read().decode() else 1)" >nul 2>nul
        if errorlevel 1 (
            ping -n 2 127.0.0.1 >nul
        ) else set "READY=1"
    )
)
if not defined READY (
    echo [start] backend NOT ready in ~30s - see basic\uvicorn.log
    pause
    exit /b 1
)
echo [start] backend OK  http://127.0.0.1:8001
ping -n 4 127.0.0.1 >nul
start "" http://localhost:5174
echo [start] done. stop with: scripts\stop_all.bat
pause
exit /b 0

:trypython
rem %~1 = candidate python.exe; smoke-test it, take it only if it really runs
"%~1" -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0
