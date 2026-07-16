@echo off
setlocal

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "APP_SCRIPT=%APP_DIR%\webui_server.py"
set "LOG_DIR=%APP_DIR%\logs"
set "APP_URL=http://127.0.0.1:8001/"
set "MODE=%~1"

if not exist "%APP_SCRIPT%" (
    echo [ERROR] Cannot find "%APP_SCRIPT%"
    exit /b 1
)

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%" >nul 2>nul
)

set "PYTHON_EXE="
set "PYTHON_ARGS="

call :try_python "%APP_DIR%\.venv\Scripts\python.exe"
call :try_python "%APP_DIR%\venv\Scripts\python.exe"
call :try_python "D:\anaconda3\python.exe"
call :try_python "D:\miniconda3\python.exe"
call :try_python "C:\Python314\python.exe"
call :try_py_launcher "%SystemRoot%\py.exe" -3

if not defined PYTHON_EXE (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined PYTHON_EXE (
            call :try_python "%%I"
        )
    )
)

if not defined PYTHON_EXE (
    echo [ERROR] Python with FastAPI was not found.
    exit /b 1
)

if /i "%MODE%"=="check" (
    echo [OK] APP_SCRIPT=%APP_SCRIPT%
    echo [OK] PYTHON="%PYTHON_EXE%" %PYTHON_ARGS%
    echo [OK] LOG_DIR=%LOG_DIR%
    exit /b 0
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>nul
if "%errorlevel%"=="0" (
    echo [INFO] ATEQ WebUI is already running: %APP_URL%
    if /i "%MODE%"=="open" (
        start "" "%APP_URL%"
    )
    exit /b 0
)

set "PS_ARGS='%APP_SCRIPT%'"
if defined PYTHON_ARGS (
    set "PS_ARGS='%PYTHON_ARGS%','%APP_SCRIPT%'"
)

powershell -NoProfile -Command "Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList @(%PS_ARGS%) -WorkingDirectory '%APP_DIR%' -WindowStyle Minimized" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Failed to start ATEQ WebUI.
    exit /b 1
)

if /i "%MODE%"=="open" (
    timeout /t 3 /nobreak >nul
    start "" "%APP_URL%"
)

exit /b 0

:try_python
if defined PYTHON_EXE goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import fastapi" >nul 2>nul
if errorlevel 1 goto :eof
set "PYTHON_EXE=%~1"
set "PYTHON_ARGS="
goto :eof

:try_py_launcher
if defined PYTHON_EXE goto :eof
if not exist "%~1" goto :eof
"%~1" %2 -c "import fastapi" >nul 2>nul
if errorlevel 1 goto :eof
set "PYTHON_EXE=%~1"
set "PYTHON_ARGS=%2"
goto :eof
