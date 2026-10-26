@echo off
rem ============================================================
rem  data-agent-lite/basic  one-key STOP (backend :8001 + frontend :5174)
rem  kills the process tree listening on each port, plus the
rem  titled console windows spawned by start_all.bat.
rem  note : keep this file pure ASCII (cmd reads bat in GBK on zh-CN)
rem ============================================================
setlocal enableextensions

call :killport 8001 backend
call :killport 5174 frontend

rem ---- titled helper windows (min windows launched by start_all) ----
taskkill /f /t /fi "WINDOWTITLE eq data-agent-backend*"  >nul 2>nul
taskkill /f /t /fi "WINDOWTITLE eq data-agent-frontend*" >nul 2>nul

echo [stop] done. (any "not found" above just means that part was already stopped)
pause
exit /b 0

:killport
rem %1 = port, %2 = label. netstat col layout: proto laddr faddr state PID
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%1 .*LISTENING"') do (
    echo [stop] %2 pid %%p
    taskkill /f /t /pid %%p >nul 2>nul
)
exit /b 0
