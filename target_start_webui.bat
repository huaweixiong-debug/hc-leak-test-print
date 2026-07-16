@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Starting ATEQ Print Door Web UI on this target computer...
echo PLC communication: Snap7 TCP/IP 192.168.2.1, M26.0.
echo.
echo Stopping old Web UI on port 8001 if it is still running...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do taskkill /F /PID %%P >nul 2>nul
set "SCAN_SERIAL_PORT=COM4"
set "SCAN_SERIAL_BAUDRATE=9600"
set "SCAN_SERIAL_BYTESIZE=8"
set "SCAN_SERIAL_PARITY=N"
set "SCAN_SERIAL_STOPBITS=1"
set "PLC_WRITE_BACKEND=snap7"
set "PLC_S7_IP=192.168.2.1"
set "PLC_S7_RACK=0"
set "PLC_S7_SLOT=1"
set "PLC_M_BYTE=26"
set "PLC_M_BIT=0"
python webui_server.py
pause
