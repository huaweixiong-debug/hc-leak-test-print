@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Starting ATEQ Print Door Web UI on this target computer...
echo ATEQ communication: COM1, station 255.
echo.
echo Stopping old Web UI on port 8001 if it is still running...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do taskkill /F /PID %%P >nul 2>nul
set "SCAN_SERIAL_PORT=COM4"
set "SCAN_SERIAL_BAUDRATE=9600"
set "SCAN_SERIAL_BYTESIZE=8"
set "SCAN_SERIAL_PARITY=N"
set "SCAN_SERIAL_STOPBITS=1"
set "ATEQ_SERIAL_PORT=COM1"
set "ATEQ_STATION_ID=255"
set "ATEQ_BAUDRATE=9600"
set "ATEQ_BYTESIZE=8"
set "ATEQ_PARITY=E"
set "ATEQ_STOPBITS=1"
".venv\Scripts\python.exe" webui_server.py
