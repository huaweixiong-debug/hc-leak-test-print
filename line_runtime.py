#!/usr/bin/env python3
"""Runtime switches for scan handling and label printing."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = Path(os.environ.get("ATEQ_LINE_SETTINGS", ROOT / "line_settings.json"))

DEFAULT_SETTINGS = {
    "scan_required": True,
    "printer_enabled": True,
}

_LOCK = threading.RLock()
_RUNTIME_STARTED = False

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
        with SETTINGS_PATH.open("r", encoding="utf-8-sig") as settings_file:
            raw = json.load(settings_file)
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
    with _LOCK:
        settings = _read_settings_unlocked()
        if scan_required is not None:
            settings["scan_required"] = bool(scan_required)
        if printer_enabled is not None:
            settings["printer_enabled"] = bool(printer_enabled)
        _write_settings_unlocked(settings)
        logger.info("Updated line settings: %s", settings)
        return settings


def mark_scan_qualified(qr_code: str = "") -> dict[str, Any]:
    """Validate the scan gate without writing to any external controller."""
    settings = get_line_settings()
    if settings["scan_required"] and not qr_code.strip():
        raise LineRuntimeError("扫码模式下二维码不能为空")
    return {
        "scan_required": settings["scan_required"],
        "stepcode_armed": True,
        "trigger_mode": "stepcode_edge",
    }


def reset_scan_gate_after_test() -> None:
    """Compatibility no-op: scan completion has no PLC output to reset."""


def start_line_runtime() -> None:
    global _RUNTIME_STARTED
    with _LOCK:
        if _RUNTIME_STARTED:
            return
        _RUNTIME_STARTED = True
    logger.info("Line runtime started without PLC integration")


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
    print(get_line_settings())
