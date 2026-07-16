@echo off
setlocal

set "SOURCE_VBS=%~dp0start_ateq_webui_silent.vbs"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET_LNK=%STARTUP_DIR%\start_ateq_webui.lnk"
set "OLD_VBS=%STARTUP_DIR%\start_ateq_webui_silent.vbs"
set "OLD_BAT=%STARTUP_DIR%\start_ateq_webui.bat"
set "OLD_TEST_LNK=%STARTUP_DIR%\start_webui_silent - 快捷方式.lnk"

if not exist "%SOURCE_VBS%" (
    echo [ERROR] Cannot find "%SOURCE_VBS%"
    exit /b 1
)

if not exist "%STARTUP_DIR%" (
    echo [ERROR] Windows Startup folder not found: "%STARTUP_DIR%"
    exit /b 1
)

set "ATEQ_STARTUP_LNK=%TARGET_LNK%"
set "ATEQ_STARTUP_VBS=%SOURCE_VBS%"
set "ATEQ_STARTUP_WORKDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ATEQ_STARTUP_LNK); $s.TargetPath=Join-Path $env:SystemRoot 'System32\wscript.exe'; $s.Arguments=[char]34+$env:ATEQ_STARTUP_VBS+[char]34; $s.WorkingDirectory=$env:ATEQ_STARTUP_WORKDIR.TrimEnd('\'); $s.Save()" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Failed to create startup shortcut.
    exit /b 1
)

if exist "%OLD_VBS%" del /F /Q "%OLD_VBS%" >nul 2>nul
if exist "%OLD_BAT%" del /F /Q "%OLD_BAT%" >nul 2>nul
if exist "%OLD_TEST_LNK%" del /F /Q "%OLD_TEST_LNK%" >nul 2>nul

echo [OK] Installed silent startup to Windows Startup:
echo %TARGET_LNK%
exit /b 0
