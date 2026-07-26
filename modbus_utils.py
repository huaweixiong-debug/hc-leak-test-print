#!/usr/bin/env python3
"""
ATEQ Modbus 通信工具模块
提供线程安全的 Modbus RTU 串口通信功能
"""

import atexit
import os
import socket
import threading
import time
from typing import Optional

import serial

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = int(os.environ.get('ATEQ_STATION_ID', '255'))
SERIAL_PORT = os.environ.get('ATEQ_SERIAL_PORT', 'COM1')
BAUDRATE = int(os.environ.get('ATEQ_BAUDRATE', '9600'))
BYTESIZE = int(os.environ.get('ATEQ_BYTESIZE', '8'))
PARITY = os.environ.get('ATEQ_PARITY', 'E')
STOPBITS = int(os.environ.get('ATEQ_STOPBITS', '1'))
TRANSPORT = os.environ.get('ATEQ_MODBUS_TRANSPORT', 'serial').lower()
REALTIME_ADDRESS = 0x30
REALTIME_REGISTER_COUNT = 13
REALTIME_CACHE_SECONDS = float(os.environ.get('ATEQ_REALTIME_CACHE_SECONDS', '0.25'))
SERIAL_ERROR_COOLDOWN_SECONDS = float(
    os.environ.get('ATEQ_SERIAL_ERROR_COOLDOWN_SECONDS', '1.0')
)

# 全局互斥锁，确保Modbus操作的线程安全
# 所有读取和写入操作必须通过这个锁进行互斥
modbus_lock = threading.Lock()
_serial_connection = None
_last_serial_issue_at = 0.0
_serial_cooldown_until = 0.0
_realtime_cache_response = None
_realtime_cache_at = 0.0

def modbus_crc(data_hex):
    """计算 Modbus RTU CRC16 校验码"""
    data = bytes.fromhex(data_hex)
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return f"{crc & 0xFF:02X}{(crc >> 8) & 0xFF:02X}"

def send_raw(data_hex, timeout=3):
    """发送原始十六进制数据并返回响应（线程安全）"""
    with modbus_lock:
        try:
            if TRANSPORT == 'tcp':
                return _send_raw_tcp(data_hex, timeout)
            return _send_raw_serial(data_hex, timeout)
        except Exception as exc:
            # Device-driver errors must not escape into the HTTP/API callers.
            _log_serial_issue(f"{SERIAL_PORT} unexpected error: {exc}")
            _close_serial_connection()
            return None


def _send_raw_tcp(data_hex, timeout=3):
    """通过旧 TCP 桥接发送 Modbus RTU 帧。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        sock.sendall(data)
        response = sock.recv(1024)
        return response.hex().upper()
    except Exception:
        return None
    finally:
        sock.close()


def _send_raw_serial(data_hex, timeout=3):
    """按配置的串口参数发送 Modbus RTU 帧。"""
    global _serial_cooldown_until

    if time.monotonic() < _serial_cooldown_until:
        return None

    data = bytes.fromhex(data_hex)
    expected_length = _expected_response_length(data)
    attempts = 2 if len(data) > 1 and data[1] in (0x01, 0x02, 0x03, 0x04) else 1

    for attempt in range(attempts):
        try:
            ser = _get_serial_connection(timeout)
            # This is the only Modbus client and all requests are serialized by
            # modbus_lock. PurgeComm on the target's legacy COM driver eventually
            # fails with WinError 31, so a healthy persistent port is not purged
            # before every request. Invalid/partial responses close the port below,
            # which also discards any stale bytes before the next request.
            ser.write(data)
            ser.flush()
            time.sleep(0.05)
            response = ser.read(expected_length)

            if response and _is_valid_response(data, response):
                _serial_cooldown_until = 0.0
                return response.hex().upper()

            if response:
                _log_serial_issue(f"invalid response: {response.hex().upper()}")
                _close_serial_connection()
            elif attempt + 1 >= attempts:
                _log_serial_issue(f"no response on {SERIAL_PORT}")
                _close_serial_connection()
        except Exception as exc:
            _log_serial_issue(f"{SERIAL_PORT} error: {exc}")
            _close_serial_connection()
            if getattr(exc, "winerror", None) == 31:
                # ERROR_GEN_FAILURE means this handle can no longer perform I/O.
                # Do not hammer the legacy driver with an immediate retry.
                _serial_cooldown_until = (
                    time.monotonic() + SERIAL_ERROR_COOLDOWN_SECONDS
                )
                break

        if attempt + 1 < attempts:
            time.sleep(0.1)

    return None


def _get_serial_connection(timeout):
    global _serial_connection

    if _serial_connection is None or not _serial_connection.is_open:
        _close_serial_connection()
        _serial_connection = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUDRATE,
            bytesize=BYTESIZE,
            parity=_serial_parity(PARITY),
            stopbits=STOPBITS,
            timeout=timeout,
            write_timeout=timeout,
        )
    else:
        _serial_connection.timeout = timeout
        _serial_connection.write_timeout = timeout

    return _serial_connection


def _close_serial_connection():
    global _serial_connection

    try:
        if _serial_connection is not None and _serial_connection.is_open:
            _serial_connection.close()
    except Exception:
        pass
    finally:
        _serial_connection = None


def _is_valid_response(request, response):
    if len(response) < 5 or response[0] != request[0]:
        return False

    function_code = request[1]
    if response[1] not in (function_code, function_code | 0x80):
        return False

    expected_crc = bytes.fromhex(modbus_crc(response[:-2].hex()))
    return response[-2:] == expected_crc


def _log_serial_issue(message):
    global _last_serial_issue_at

    now = time.monotonic()
    if now - _last_serial_issue_at >= 5:
        print(f"[ATEQ-SERIAL] {message}")
        _last_serial_issue_at = now


atexit.register(_close_serial_connection)


def _serial_parity(value):
    value = (value or 'E').upper()
    return {
        'E': serial.PARITY_EVEN,
        'N': serial.PARITY_NONE,
        'O': serial.PARITY_ODD,
    }.get(value, serial.PARITY_EVEN)


def _expected_response_length(request):
    if len(request) < 6:
        return 1024

    function_code = request[1]
    if function_code in (0x05, 0x06, 0x0F, 0x10):
        return 8

    if function_code in (0x03, 0x04):
        count = int.from_bytes(request[4:6], byteorder='big')
        return 5 + count * 2

    if function_code in (0x01, 0x02):
        count = int.from_bytes(request[4:6], byteorder='big')
        return 5 + ((count + 7) // 8)

    return 1024


def _coil_value(value):
    if isinstance(value, bool):
        return 0xFF00 if value else 0x0000
    return 0xFF00 if value else 0x0000


def _register_value(value):
    return int(value) & 0xFFFF


def decode_realtime_step_code(response_hex: str) -> Optional[int]:
    """Decode StepCode from register 4 of the 0x30 realtime block."""
    try:
        data = bytes.fromhex(response_hex)
    except (TypeError, ValueError):
        return None
    if (
        len(data) < 13
        or data[0] != STATION_ID
        or data[1] != 0x03
        or data[2] < 10
    ):
        return None

    register_value = (data[11] << 8) | data[12]
    return ((register_value & 0xFF) << 8) | ((register_value >> 8) & 0xFF)

def read_holding_registers(address, count):
    """读取保持寄存器（线程安全）"""
    global _realtime_cache_at, _realtime_cache_response

    if address == REALTIME_ADDRESS and count == REALTIME_REGISTER_COUNT:
        with modbus_lock:
            now = time.monotonic()
            if (
                _realtime_cache_response is not None
                and now - _realtime_cache_at < REALTIME_CACHE_SECONDS
            ):
                return _realtime_cache_response

            cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
            try:
                if TRANSPORT == 'tcp':
                    response = _send_raw_tcp(cmd + modbus_crc(cmd))
                else:
                    response = _send_raw_serial(cmd + modbus_crc(cmd))
            except Exception as exc:
                _log_serial_issue(f"{SERIAL_PORT} realtime read error: {exc}")
                _close_serial_connection()
                return None
            if response:
                _realtime_cache_response = response
                _realtime_cache_at = time.monotonic()
            return response

    cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
    return send_raw(cmd + modbus_crc(cmd))

def write_single_register(address, value):
    """写单个寄存器（线程安全）"""
    cmd = f"{STATION_ID:02X}06{address:04X}{_register_value(value):04X}"
    return send_raw(cmd + modbus_crc(cmd))

def write_single_coil(address, value):
    """写单个线圈（线程安全）"""
    cmd = f"{STATION_ID:02X}05{address:04X}{_coil_value(value):04X}"
    return send_raw(cmd + modbus_crc(cmd))
