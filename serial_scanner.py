#!/usr/bin/env python3
"""Serial barcode/QR scanner support."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import serial


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "serial_scanner.log"

SCAN_PORT = os.environ.get("SCAN_SERIAL_PORT", "COM4")
SCAN_BAUDRATE = int(os.environ.get("SCAN_SERIAL_BAUDRATE", "9600"))
SCAN_BYTESIZE = int(os.environ.get("SCAN_SERIAL_BYTESIZE", "8"))
SCAN_PARITY = os.environ.get("SCAN_SERIAL_PARITY", "N").upper()
SCAN_STOPBITS = float(os.environ.get("SCAN_SERIAL_STOPBITS", "1"))
SCAN_TIMEOUT = float(os.environ.get("SCAN_SERIAL_TIMEOUT", "0.05"))
SCAN_IDLE_SECONDS = float(os.environ.get("SCAN_IDLE_SECONDS", "0.45"))
SCAN_RECONNECT_SECONDS = float(os.environ.get("SCAN_RECONNECT_SECONDS", "2.0"))
SCAN_MIN_REPEAT_UNIT = int(os.environ.get("SCAN_MIN_REPEAT_UNIT", "4"))

logger = logging.getLogger("SerialScanner")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)


SCAN_DELIMITERS = {10, 13, 9}
SCAN_STRIP_BYTES = b"\x00\x02\x03\r\n\t "


@dataclass
class ScanState:
    seq: int = 0
    code: str = ""
    raw_hex: str = ""
    timestamp: str = ""
    port: str = SCAN_PORT
    connected: bool = False
    error: str = ""


_LOCK = threading.RLock()
_STATE = ScanState()
_STARTED = False


def _serial_parity(value: str):
    return {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }.get((value or "N").upper(), serial.PARITY_NONE)


def _clean_scan_bytes(raw: bytes) -> bytes:
    return raw.strip(SCAN_STRIP_BYTES)


def _collapse_repeated_scan(raw: bytes) -> tuple[bytes, int]:
    cleaned = _clean_scan_bytes(raw)
    size = len(cleaned)
    if size < max(SCAN_MIN_REPEAT_UNIT * 2, 8):
        return cleaned, 1

    max_unit_length = size // 2
    for unit_length in range(SCAN_MIN_REPEAT_UNIT, max_unit_length + 1):
        if size % unit_length != 0:
            continue

        repeat_count = size // unit_length
        if repeat_count < 2:
            continue

        unit = cleaned[:unit_length]
        if unit * repeat_count == cleaned:
            return unit, repeat_count

    return cleaned, 1


def _decode_scan(raw: bytes) -> str:
    cleaned = _clean_scan_bytes(raw)
    for encoding in ("utf-8", "gbk", "latin1"):
        try:
            return cleaned.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return cleaned.decode("latin1", errors="replace").strip()


def _publish_scan(raw: bytes) -> None:
    normalized_raw, repeat_count = _collapse_repeated_scan(raw)
    code = _decode_scan(normalized_raw)
    if not code:
        return

    with _LOCK:
        _STATE.seq += 1
        _STATE.code = code
        _STATE.raw_hex = normalized_raw.hex(" ").upper()
        _STATE.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _STATE.port = SCAN_PORT
        _STATE.connected = True
        _STATE.error = ""
        seq = _STATE.seq

    if repeat_count > 1:
        logger.info(
            "Collapsed repeated scan payload #%s repeat=%s unit_len=%s raw=%s",
            seq,
            repeat_count,
            len(normalized_raw),
            raw.hex(" ").upper(),
        )
    logger.info("SCAN #%s len=%s code=%s raw=%s", seq, len(code), code, normalized_raw.hex(" ").upper())


def _consume_chunk(buffer: bytearray, chunk: bytes) -> None:
    for byte in chunk:
        if byte in SCAN_DELIMITERS:
            if buffer:
                _publish_scan(bytes(buffer))
                buffer.clear()
            continue
        buffer.append(byte)


def get_scanner_state() -> dict[str, Any]:
    with _LOCK:
        data = asdict(_STATE)
    data.update(
        {
            "baudrate": SCAN_BAUDRATE,
            "bytesize": SCAN_BYTESIZE,
            "parity": SCAN_PARITY,
            "stopbits": SCAN_STOPBITS,
        }
    )
    return data


def start_serial_scanner() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    thread = threading.Thread(target=_scanner_loop, name="SerialScanner", daemon=True)
    thread.start()


def _scanner_loop() -> None:
    while True:
        try:
            _read_serial_forever()
        except Exception as exc:
            with _LOCK:
                _STATE.connected = False
                _STATE.error = str(exc)
                _STATE.port = SCAN_PORT
            logger.warning("Scanner serial loop failed: %s", exc)
            time.sleep(SCAN_RECONNECT_SECONDS)


def _read_serial_forever() -> None:
    buffer = bytearray()
    last_byte_time = 0.0

    with serial.Serial(
        port=SCAN_PORT,
        baudrate=SCAN_BAUDRATE,
        bytesize=SCAN_BYTESIZE,
        parity=_serial_parity(SCAN_PARITY),
        stopbits=SCAN_STOPBITS,
        timeout=SCAN_TIMEOUT,
    ) as ser:
        ser.reset_input_buffer()
        with _LOCK:
            _STATE.connected = True
            _STATE.error = ""
            _STATE.port = SCAN_PORT
        logger.info(
            "Scanner opened: %s %s %s%s%s",
            SCAN_PORT,
            SCAN_BAUDRATE,
            SCAN_BYTESIZE,
            SCAN_PARITY,
            SCAN_STOPBITS,
        )

        while True:
            chunk = ser.read(64)
            now = time.monotonic()

            if chunk:
                if buffer and last_byte_time and now - last_byte_time >= SCAN_IDLE_SECONDS:
                    _publish_scan(bytes(buffer))
                    buffer.clear()
                _consume_chunk(buffer, chunk)
                last_byte_time = now
                continue

            if buffer and last_byte_time and now - last_byte_time >= SCAN_IDLE_SECONDS:
                _publish_scan(bytes(buffer))
                buffer.clear()
