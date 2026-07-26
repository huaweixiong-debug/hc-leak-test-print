"""Publish the latest completed test as one text file per label field."""

import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path


LABEL_DATA_DIR = Path(os.environ.get("ATEQ_LABEL_DATA_DIR", r"D:\Data"))

LABEL_DATA_FILENAMES = {
    "product_model": "产品型号.txt",
    "completed_at": "日期时间.txt",
    "daily_sequence": "当日序号.txt",
    "pressure1": "测试压力1.txt",
    "leak1": "泄漏量1.txt",
    "pressure2": "测试压力2.txt",
    "leak2": "泄漏量2.txt",
    "result": "结果.txt",
    "operator": "员工.txt",
}


def _format_measurement(value):
    if value is None:
        return ""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()

    if not math.isfinite(number):
        return ""

    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _format_measurement_with_unit(test_data, value_key, unit_key):
    value = _format_measurement(test_data.get(value_key))
    if not value:
        return ""
    unit = str(test_data.get(unit_key) or "").strip()
    unit = unit.replace("³", "3").replace("²", "2")
    return f"{value}{unit}"


def _atomic_write_text(target, value):
    payload = (str(value or "") + "\r\n").encode("utf-8-sig")
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )

    temporary.write_bytes(payload)
    for attempt in range(3):
        try:
            os.replace(str(temporary), str(target))
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)
    raise RuntimeError(f"Failed to publish label data file: {target}")


def write_label_data_files(
    test_data,
    product_model,
    operator,
    daily_sequence,
    overall_result,
    completed_at=None,
    target_dir=None,
):
    """Atomically replace all nine BarTender label field files."""
    directory = Path(target_dir) if target_dir else LABEL_DATA_DIR
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = completed_at or datetime.now()

    values = {
        "product_model": str(product_model or "").strip(),
        "completed_at": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "daily_sequence": str(daily_sequence or "").strip(),
        "pressure1": _format_measurement_with_unit(
            test_data, "pressure1", "pressure1_unit"
        ),
        "leak1": _format_measurement_with_unit(test_data, "leak1", "leak1_unit"),
        "pressure2": _format_measurement_with_unit(
            test_data, "pressure2", "pressure2_unit"
        ),
        "leak2": _format_measurement_with_unit(test_data, "leak2", "leak2_unit"),
        "result": str(overall_result or "UNKNOWN").strip().upper(),
        "operator": str(operator or "").strip(),
    }

    published = {}
    for field_name, filename in LABEL_DATA_FILENAMES.items():
        target = directory / filename
        _atomic_write_text(target, values[field_name])
        published[field_name] = str(target)
    return published
