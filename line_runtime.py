#!/usr/bin/env python3
"""
Runtime switches for scan handling, printer use, and Siemens S7-200 SMART PLC I/O.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = Path(os.environ.get("ATEQ_LINE_SETTINGS", ROOT / "line_settings.json"))
PLC_HELPER = ROOT / "plc_pcaccess_m26.vbs"

DEFAULT_SETTINGS = {
    "scan_required": True,
    "printer_enabled": True,
}

DEFAULT_OPC_SERVER = "S7200Smart.OPCServer"
DEFAULT_OPC_ITEM = "2:0.0.0.0:0201:0201,M26.0,BOOL,RW"
DEFAULT_OPC_SYMBOL_ITEM = "MWSMART.NewPLC.扫码ok"
DEFAULT_OPC_ADDRESS_ITEM = "2:0.0.0.0:0201:0201,M26.0,BOOL,RW"
DEFAULT_OPC_FULL_ITEM = "MWSMART:2:0.0.0.0:0201:0201,M26.0,BOOL,RW"
DEFAULT_OPC_FULL_ITEM_WITH_LIMITS = "MWSMART:2:0.0.0.0:0201:0201,M26.0,BOOL,RW,0.0000000,0.0000000"
DEFAULT_OPC_ITEMS_READ = ";".join(
    [
        DEFAULT_OPC_SYMBOL_ITEM,
        DEFAULT_OPC_ADDRESS_ITEM,
    ]
)
DEFAULT_OPC_ITEMS_ON = ";".join(
    [
        DEFAULT_OPC_ADDRESS_ITEM,
        DEFAULT_OPC_FULL_ITEM,
        DEFAULT_OPC_FULL_ITEM_WITH_LIMITS,
    ]
)
DEFAULT_OPC_ITEMS_OFF = ";".join(
    [
        DEFAULT_OPC_SYMBOL_ITEM,
        DEFAULT_OPC_ADDRESS_ITEM,
        DEFAULT_OPC_FULL_ITEM,
        DEFAULT_OPC_FULL_ITEM_WITH_LIMITS,
    ]
)
PLC_BACKEND = os.environ.get("PLC_WRITE_BACKEND", "snap7").strip().lower()
PLC_S7_IP = os.environ.get("PLC_S7_IP", "192.168.2.1")
PLC_S7_RACK = int(os.environ.get("PLC_S7_RACK", "0"))
PLC_S7_SLOT = int(os.environ.get("PLC_S7_SLOT", "1"))
PLC_M_BYTE = int(os.environ.get("PLC_M_BYTE", "26"))
PLC_M_BIT = int(os.environ.get("PLC_M_BIT", "0"))
PLC_CONTROL_M_BYTE = int(os.environ.get("PLC_CONTROL_M_BYTE", "25"))
PLC_START_M_BIT = int(os.environ.get("PLC_START_M_BIT", "0"))
PLC_STOP_M_BIT = int(os.environ.get("PLC_STOP_M_BIT", "1"))

_LOCK = threading.RLock()
_PLC_LOCK = threading.RLock()
_KEEPALIVE_STARTED = False
_LAST_KEEPALIVE_ERROR = ""
_LAST_KEEPALIVE_ERROR_AT = 0.0
_S7_CLIENT = None
_S7_MODULE = None
_S7_UTIL = None

logger = logging.getLogger("LineRuntime")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(ROOT / "line_runtime.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)


class LineRuntimeError(RuntimeError):
    pass


def _read_settings_unlocked() -> dict[str, bool]:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULT_SETTINGS)

    try:
        with SETTINGS_PATH.open("r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning("Failed to read settings, using defaults: %s", exc)
        return dict(DEFAULT_SETTINGS)

    settings = dict(DEFAULT_SETTINGS)
    for key in settings:
        if key in raw:
            settings[key] = bool(raw[key])
    return settings


def _write_settings_unlocked(settings: dict[str, bool]) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_line_settings() -> dict[str, bool]:
    with _LOCK:
        return _read_settings_unlocked()


def update_line_settings(
    scan_required: bool | None = None,
    printer_enabled: bool | None = None,
) -> dict[str, bool]:
    plc_target: bool | None = None
    with _PLC_LOCK:
        with _LOCK:
            settings = _read_settings_unlocked()
            old_scan_required = settings["scan_required"]

            if scan_required is not None:
                settings["scan_required"] = bool(scan_required)
            if printer_enabled is not None:
                settings["printer_enabled"] = bool(printer_enabled)

            if scan_required is not None and settings["scan_required"] != old_scan_required:
                plc_target = not settings["scan_required"]

            _write_settings_unlocked(settings)
            logger.info("Updated line settings: %s", settings)

        if plc_target is not None:
            set_m26_0(plc_target)

        return settings


def _cscript_path() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for candidate in (windir / "SysWOW64" / "cscript.exe", windir / "System32" / "cscript.exe"):
        if candidate.exists():
            return candidate
    raise LineRuntimeError("Windows Script Host cscript.exe was not found.")


def _run_plc_helper(action: str, timeout: int | None = None, read_source: str | None = None) -> str:
    if not PLC_HELPER.exists():
        raise LineRuntimeError(f"{PLC_HELPER.name} was not found beside line_runtime.py.")

    if timeout is None:
        default_timeout = "10" if action == "write_off" else "20"
        timeout = int(os.environ.get("PLC_OPC_TIMEOUT_SECONDS", default_timeout))

    env = os.environ.copy()
    env.setdefault("PLC_OPC_SERVER", DEFAULT_OPC_SERVER)
    env.setdefault("PLC_OPC_SERVERS", DEFAULT_OPC_SERVER)
    if action == "write_on":
        default_items = os.environ.get("PLC_OPC_ITEMS_ON", DEFAULT_OPC_ITEMS_ON)
    elif action == "write_off":
        default_items = os.environ.get("PLC_OPC_ITEMS_OFF", DEFAULT_OPC_ITEMS_OFF)
    else:
        default_items = os.environ.get("PLC_OPC_ITEMS_READ", DEFAULT_OPC_ITEMS_READ)
    env.setdefault("PLC_OPC_ITEMS", default_items)
    if action in ("write_on", "write_off"):
        env.setdefault("PLC_OPC_RETRIES", "1")
    else:
        env.setdefault("PLC_OPC_RETRIES", "3")
        env["PLC_OPC_READ_SOURCE"] = (read_source or "cache").strip().lower() or "cache"
    env["PLC_OPC_ACTION"] = action

    with _PLC_LOCK:
        try:
            result = subprocess.run(
                [str(_cscript_path()), "//nologo", str(PLC_HELPER)],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or exc.stderr or "")
            if isinstance(partial, bytes):
                partial = partial.decode(errors="ignore")
            detail = partial.strip()
            if detail:
                detail = " Last helper output: " + detail
            raise LineRuntimeError(
                f"{PLC_HELPER.name} timed out during {action}. "
                "PC Access SMART may be holding the OPC call; close/reopen the .sa file if it repeats."
                + detail
            ) from exc

        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if result.returncode != 0:
            message = error or output or f"exit code {result.returncode}"
            if "OPC Automation Wrapper is not registered" in message:
                message += (
                    " Register the 32-bit OPC DA Automation Wrapper on the PLC computer. "
                    "For PPI, also open S7-200 PC Access SMART and load the working .sa file."
                )
            raise LineRuntimeError(message)

        return output


def set_m26_0(enabled: bool) -> str:
    if enabled:
        raise LineRuntimeError(
            "M26.0 legacy hardware-I/O start is disabled; use UI start or PLC M25.0"
        )
    with _PLC_LOCK:
        return _set_m26_0_unlocked(enabled)


def _set_m26_0_unlocked(enabled: bool) -> str:
    errors: list[str] = []

    if PLC_BACKEND in ("auto", "opc", "pcaccess", "pc_access"):
        try:
            action = "write_on" if enabled else "write_off"
            output = _run_plc_helper(action)
            logger.info("M26.0 set to %s through OPC: %s", "ON" if enabled else "OFF", output)
            return output
        except Exception as exc:
            errors.append(f"OPC: {exc}")
            if PLC_BACKEND != "auto":
                raise

    if PLC_BACKEND in ("auto", "s7", "snap7"):
        try:
            output = _run_snap7_with_retry(
                lambda: _set_m_bit_snap7(enabled),
                f"write M{PLC_M_BYTE}.{PLC_M_BIT}",
            )
            logger.info("M26.0 set to %s through snap7: %s", "ON" if enabled else "OFF", output)
            return output
        except Exception as exc:
            errors.append(f"snap7: {exc}")

    raise LineRuntimeError("M26.0 write failed. " + " | ".join(errors))


def read_m26_0(read_source: str = "cache") -> str:
    with _PLC_LOCK:
        return _read_m26_0_unlocked(read_source)


def _read_m26_0_unlocked(read_source: str = "cache") -> str:
    errors: list[str] = []

    if PLC_BACKEND in ("auto", "s7", "snap7"):
        try:
            output = _run_snap7_with_retry(
                _read_m_bit_snap7,
                f"read M{PLC_M_BYTE}.{PLC_M_BIT}",
            )
            logger.info("M26.0 read through snap7: %s", output)
            return output
        except Exception as exc:
            errors.append(f"snap7: {exc}")
            if PLC_BACKEND != "auto":
                raise

    if PLC_BACKEND in ("auto", "opc", "pcaccess", "pc_access"):
        try:
            return _run_plc_helper(
                "read",
                timeout=int(os.environ.get("PLC_OPC_READ_TIMEOUT_SECONDS", "6")),
                read_source=read_source,
            )
        except Exception as exc:
            errors.append(f"OPC: {exc}")
            if PLC_BACKEND != "auto":
                raise

    raise LineRuntimeError("M26.0 read failed. " + " | ".join(errors))


def _run_snap7_with_retry(operation, action: str):
    retries = max(1, int(os.environ.get("PLC_S7_RETRIES", "3")))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            _reset_snap7_client_unlocked()
            if attempt >= retries:
                break
            logger.warning(
                "Transient snap7 %s failure (%s/%s): %s",
                action,
                attempt,
                retries,
                exc,
            )
            time.sleep(0.15 * attempt)
    raise last_error


def _snap7_mk_area(snap7_module):
    for attr in ("type", "types"):
        module = getattr(snap7_module, attr, None)
        areas = getattr(module, "Areas", None) if module is not None else None
        mk = getattr(areas, "MK", None) if areas is not None else None
        if mk is not None:
            return mk
    raise LineRuntimeError("python-snap7 Areas.MK is not available")


def _reset_snap7_client_unlocked() -> None:
    global _S7_CLIENT
    client = _S7_CLIENT
    _S7_CLIENT = None
    if client is None:
        return
    try:
        if client.get_connected():
            client.disconnect()
    except Exception:
        pass
    destroy = getattr(client, "destroy", None)
    if callable(destroy):
        try:
            destroy()
        except Exception:
            pass


def _get_snap7_client_unlocked():
    global _S7_CLIENT, _S7_MODULE, _S7_UTIL
    try:
        import snap7
        from snap7 import util
    except Exception as exc:
        raise LineRuntimeError(f"python-snap7 is not available: {exc}") from exc

    _S7_MODULE = snap7
    _S7_UTIL = util
    if _S7_CLIENT is not None and _S7_CLIENT.get_connected():
        return snap7, util, _S7_CLIENT

    _reset_snap7_client_unlocked()
    client = snap7.client.Client()
    try:
        client.connect(PLC_S7_IP, PLC_S7_RACK, PLC_S7_SLOT)
    except Exception as exc:
        try:
            client.destroy()
        except Exception:
            pass
        raise LineRuntimeError(
            f"snap7 connect failed: {PLC_S7_IP} rack={PLC_S7_RACK} slot={PLC_S7_SLOT}: {exc}"
        ) from exc
    if not client.get_connected():
        raise LineRuntimeError(f"could not connect to PLC {PLC_S7_IP}")
    _S7_CLIENT = client
    logger.info("Persistent snap7 connection established: %s rack=%s slot=%s", PLC_S7_IP, PLC_S7_RACK, PLC_S7_SLOT)
    return snap7, util, client


def _read_m_byte_snap7(byte_index: int):
    snap7, _, plc = _get_snap7_client_unlocked()
    return plc.read_area(_snap7_mk_area(snap7), 0, byte_index, 1)


def _set_m_bit_snap7(enabled: bool) -> str:
    snap7, util, plc = _get_snap7_client_unlocked()
    area_mk = _snap7_mk_area(snap7)
    data = plc.read_area(area_mk, 0, PLC_M_BYTE, 1)
    util.set_bool(data, 0, PLC_M_BIT, bool(enabled))
    plc.write_area(area_mk, 0, PLC_M_BYTE, data)
    return f"S7 WRITE {'ON' if enabled else 'OFF'} OK: {PLC_S7_IP} M{PLC_M_BYTE}.{PLC_M_BIT}"


def _read_m_bit_snap7() -> str:
    _, util, _ = _get_snap7_client_unlocked()
    data = _read_m_byte_snap7(PLC_M_BYTE)
    value = bool(util.get_bool(data, 0, PLC_M_BIT))
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"READ snap7: raw={value}, bool={'ON' if value else 'OFF'}, "
        f"quality=192, ip={PLC_S7_IP}, address=M{PLC_M_BYTE}.{PLC_M_BIT}, "
        f"timestamp={timestamp}"
    )


def read_plc_control_inputs() -> dict[str, Any]:
    """Read PLC command bits M25.0 (start) and M25.1 (stop)."""
    with _PLC_LOCK:
        data = _run_snap7_with_retry(
            lambda: _read_m_byte_snap7(PLC_CONTROL_M_BYTE),
            f"read M{PLC_CONTROL_M_BYTE}",
        )
        raw = int(data[0])
        return {
            "start": bool(raw & (1 << PLC_START_M_BIT)),
            "stop": bool(raw & (1 << PLC_STOP_M_BIT)),
            "raw": raw,
            "quality": 192,
            "timestamp": time.time(),
        }


def mark_scan_qualified(qr_code: str = "") -> dict[str, Any]:
    settings = get_line_settings()
    if settings["scan_required"] and not qr_code.strip():
        raise LineRuntimeError("扫码模式下二维码不能为空")

    # M26.0 was the legacy hardware-I/O start gate. Keep it low now that
    # M25.0/M25.1 invoke the same Modbus start/stop path as the UI buttons.
    output = "M26.0 legacy hardware-I/O start disabled"
    try:
        output = set_m26_0(False)
    except Exception as exc:
        logger.warning("Could not enforce legacy M26.0 OFF after scan: %s", exc)
        output = f"M26.0 OFF enforcement failed: {exc}"
    return {
        "m26_on": False,
        "legacy_io_disabled": True,
        "scan_required": settings["scan_required"],
        "output": output,
    }


def reset_scan_gate_after_test() -> None:
    set_m26_0(False)


def start_line_runtime() -> None:
    global _KEEPALIVE_STARTED
    with _LOCK:
        if _KEEPALIVE_STARTED:
            return
        _KEEPALIVE_STARTED = True

    try:
        set_m26_0(False)
        logger.info("Legacy M26.0 hardware-I/O start disabled; output forced OFF")
    except Exception as exc:
        logger.warning("Could not force legacy M26.0 OFF during startup: %s", exc)


def _keepalive_loop() -> None:
    global _LAST_KEEPALIVE_ERROR, _LAST_KEEPALIVE_ERROR_AT
    interval = float(os.environ.get("PLC_M26_KEEPALIVE_SECONDS", "2.0"))
    error_log_interval = float(os.environ.get("PLC_KEEPALIVE_ERROR_LOG_SECONDS", "30.0"))
    while True:
        try:
            settings = get_line_settings()
            if not settings["scan_required"]:
                with _PLC_LOCK:
                    if not get_line_settings()["scan_required"]:
                        _set_m26_0_unlocked(True)
                _LAST_KEEPALIVE_ERROR = ""
                _LAST_KEEPALIVE_ERROR_AT = 0.0
        except Exception as exc:
            message = str(exc)
            now = time.time()
            if message != _LAST_KEEPALIVE_ERROR or now - _LAST_KEEPALIVE_ERROR_AT >= error_log_interval:
                logger.warning("M26.0 keepalive failed: %s", exc)
                _LAST_KEEPALIVE_ERROR = message
                _LAST_KEEPALIVE_ERROR_AT = now
        time.sleep(max(0.5, interval))


def print_label_if_enabled(
    template_name: str,
    serial_number: str,
    product_model: str,
    qr_code: str | None = None,
    supplier_code: str = "",
    sequence_code: str | None = None,
) -> dict[str, Any]:
    settings = get_line_settings()
    if not settings["printer_enabled"]:
        return {"success": True, "printed": False, "message": "打印机使用已关闭"}

    if not sequence_code:
        tail = str(serial_number or "").rsplit("-", 1)[-1]
        sequence_code = tail if tail.isdigit() else str(serial_number or "")
    template_name = (template_name or "").strip()
    if False and not template_name:
        return {"success": False, "printed": False, "message": "未配置标签模板"}

    try:
        from bartender_print import BarTenderPrinter

        ok = BarTenderPrinter().print_fixed_label_files(
            product_model=product_model or "",
            supplier_code=supplier_code or "",
            sequence_code=sequence_code or "",
            template_path=template_name or "",
        )
    except Exception as exc:
        logger.exception("Print failed")
        return {"success": False, "printed": False, "message": str(exc)}

    if ok:
        return {"success": True, "printed": True, "message": "标签打印成功"}
    return {"success": False, "printed": False, "message": "标签打印失败"}


if __name__ == "__main__":
    import sys

    action = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "status"
    try:
        if action in ("status", "settings"):
            print(get_line_settings())
        elif action in ("read", "read_m26"):
            print(read_m26_0())
        elif action in ("on", "write_on"):
            print(set_m26_0(True))
        elif action in ("off", "write_off"):
            print(set_m26_0(False))
        else:
            raise SystemExit("Usage: python line_runtime.py [status|read|on|off]")
    except Exception as exc:
        raise SystemExit(f"PLC check failed: {exc}") from exc
