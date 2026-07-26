from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import subprocess
import os
import logging
import re
import sys
import threading
from pathlib import Path

# The ATEQ unit table contains characters such as the superscript in ``cm³/min``.
# Windows service consoles commonly use GBK, which cannot encode every unit symbol.
# A diagnostic print must never abort the record-save and label-print workflow.
for console_stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(console_stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass

# 启动时终止之前的进程
PORT = 8001
PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ASSET_ROOT = PROJECT_ROOT / "web_assets"
SCANNER_INPUT_MODE = os.environ.get("SCAN_INPUT_MODE", "keyboard").strip().lower()
import time
logger = logging.getLogger("ATEQWebUI")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    webui_log_handler = logging.FileHandler(PROJECT_ROOT / "webui_runtime.log", encoding="utf-8")
    webui_log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(webui_log_handler)

def stop_previous_server(port: int) -> None:
    if os.name != "nt":
        subprocess.run(
            f"lsof -ti:{port} | xargs kill -9 2>/dev/null || true",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        print(f"Port cleanup skipped: {exc}")
        return

    current_pid = str(os.getpid())
    pids: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP":
            local_addr = parts[1]
            state = parts[3].upper()
            pid = parts[4]
            if local_addr.endswith(f":{port}") and state == "LISTENING" and pid != current_pid:
                pids.add(pid)

    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def stop_stale_project_python_processes() -> None:
    """Stop old project Python processes that may still be holding COM4."""
    if os.name != "nt":
        return

    project_path = str(PROJECT_ROOT).lower().replace("'", "''")
    script = f"""
$CurrentPid = {os.getpid()}
$ProjectPath = '{project_path}'
$Needles = @('webui_server.py', 'scan_serial_test.py', 'serial_scanner.py')
Get-CimInstance Win32_Process | ForEach-Object {{
    $proc = $_
    if ($proc.ProcessId -eq $CurrentPid) {{ return }}
    if ($proc.Name -notmatch '^(python|pythonw)\\.exe$') {{ return }}
    $cmd = [string]$proc.CommandLine
    if (-not $cmd) {{ return }}
    $lower = $cmd.ToLowerInvariant()
    $matched = $lower.Contains($ProjectPath)
    foreach ($needle in $Needles) {{
        if ($lower.Contains($needle)) {{
            $matched = $true
            break
        }}
    }}
    if ($matched) {{
        try {{
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Output ("Stopped stale scanner/webui process PID=" + $proc.ProcessId)
        }} catch {{
            Write-Output ("Could not stop PID=" + $proc.ProcessId + ": " + $_.Exception.Message)
        }}
    }}
}}
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
        )
        output = (result.stdout or result.stderr or "").strip()
        if output:
            print(output)
    except Exception as exc:
        print(f"COM4 stale process cleanup skipped: {exc}")


stop_previous_server(PORT)
stop_stale_project_python_processes()
time.sleep(0.5)

# 导入数据库模块
from database import init_database, save_test_record, get_latest_records, get_statistics_by_date, get_records_by_date, query_records, get_all_product_models
from serial_generator import generate_serial_number, get_generator

# 导入程序选择模块
from program_selector import test_executor, write_program, read_current_program, start_test, reset_device, is_test_complete

LINE_RUNTIME_IMPORT_ERROR = None
try:
    from line_runtime import (
        get_line_settings,
        mark_scan_qualified,
        start_line_runtime,
        update_line_settings,
    )
except Exception as exc:
    LINE_RUNTIME_IMPORT_ERROR = exc
    get_line_settings = None
    mark_scan_qualified = None
    start_line_runtime = None
    update_line_settings = None

SERIAL_SCANNER_IMPORT_ERROR = None
if SCANNER_INPUT_MODE == "keyboard":
    get_scanner_state = None
    start_serial_scanner = None
else:
    try:
        from serial_scanner import get_scanner_state, start_serial_scanner
    except Exception as exc:
        SERIAL_SCANNER_IMPORT_ERROR = exc
        get_scanner_state = None
        start_serial_scanner = None

# 初始化数据库
init_database()

app = FastAPI(title="ATEQ 仪器控制")
app.mount("/assets", StaticFiles(directory=str(WEB_ASSET_ROOT)), name="assets")


@app.on_event("startup")
def startup_line_runtime():
    if start_line_runtime is not None:
        start_line_runtime()
    if start_serial_scanner is not None:
        start_serial_scanner()

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATEQ 仪器控制</title>
    <script src="/assets/tailwindcss-3.4.17.js"></script>
    <script src="/assets/interact-1.10.27.min.js"></script>
    <script src="/assets/chart-4.4.9.umd.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', system-ui, sans-serif;
        }
        .control-card {
            position: absolute;
            touch-action: none;
            user-select: none;
        }
        .control-card.dragging {
            opacity: 0.9;
        }
        .drag-handle {
            cursor: grab;
        }
        .drag-handle:active {
            cursor: grabbing;
        }
        .resize-handle {
            position: absolute;
            bottom: 0;
            right: 0;
            width: 20px;
            height: 20px;
            cursor: se-resize;
            background: linear-gradient(135deg, transparent 50%, rgba(255,255,255,0.3) 50%);
            border-radius: 0 0 16px 0;
        }
        .btn {
            transition: all 0.2s ease;
        }
        .btn:hover {
            transform: translateY(-2px);
        }
        .btn-start {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4);
        }
        .btn-start:hover {
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.6);
        }
        .btn-stop {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4);
        }
        .btn-stop:hover {
            box-shadow: 0 6px 20px rgba(239, 68, 68, 0.6);
        }
        .ateq-button-row {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }
        .ateq-action-btn {
            width: 100%;
            min-width: 0;
            height: 52px;
            padding-top: 0;
            padding-bottom: 0;
        }
        .scan-toggle-btn {
            display: flex;
            align-items: center;
            min-width: 0;
            height: 52px;
            padding: 7px;
            justify-content: flex-start;
            gap: 5px;
            background: linear-gradient(135deg, #334155 0%, #27364a 100%);
            border: 1px solid rgba(148, 163, 184, 0.38);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.07), 0 6px 16px rgba(2, 6, 23, 0.22);
        }
        .scan-toggle-btn:hover {
            border-color: rgba(148, 163, 184, 0.68);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1), 0 8px 20px rgba(2, 6, 23, 0.28);
        }
        .scan-toggle-btn[aria-pressed="true"] {
            background: linear-gradient(135deg, #2563eb 0%, #0284c7 100%);
            border-color: rgba(125, 211, 252, 0.72);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.18), 0 7px 18px rgba(37, 99, 235, 0.34);
        }
        .scan-toggle-icon-wrap {
            display: grid;
            place-items: center;
            flex: 0 0 24px;
            width: 24px;
            height: 24px;
            border-radius: 7px;
            color: #cbd5e1;
            background: rgba(15, 23, 42, 0.36);
        }
        .scan-toggle-btn[aria-pressed="true"] .scan-toggle-icon-wrap {
            color: #ffffff;
            background: rgba(255, 255, 255, 0.15);
        }
        .scan-toggle-copy {
            display: flex;
            min-width: 0;
            flex: 1 1 auto;
            flex-direction: column;
            align-items: flex-start;
            line-height: 1.05;
        }
        .scan-toggle-title {
            color: #f8fafc;
            font-size: 12px;
            font-weight: 700;
            white-space: nowrap;
        }
        .scan-toggle-state {
            margin-top: 4px;
            color: #94a3b8;
            font-size: 9px;
            font-weight: 600;
            letter-spacing: 0.08em;
            white-space: nowrap;
        }
        .scan-toggle-btn[aria-pressed="true"] .scan-toggle-state {
            color: #dbeafe;
        }
        .scan-toggle-switch {
            position: relative;
            flex: 0 0 26px;
            width: 26px;
            height: 16px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.72);
            box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.35);
        }
        .scan-toggle-thumb {
            position: absolute;
            top: 3px;
            left: 3px;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #cbd5e1;
            box-shadow: 0 1px 4px rgba(2, 6, 23, 0.5);
            transition: transform 0.2s ease, background-color 0.2s ease;
        }
        .scan-toggle-btn[aria-pressed="true"] .scan-toggle-thumb {
            transform: translateX(10px);
            background: #ffffff;
        }
        
        /* 标签页样式 */
        .tab-btn {
            transition: all 0.2s ease;
        }
        .tab-btn:hover {
            background-color: rgba(255, 255, 255, 0.05);
        }
        .tab-btn.active {
            color: white;
            border-bottom-color: #3b82f6;
        }
        .tab-pane {
            display: none;
        }
        .tab-pane.active {
            display: block;
        }
        .settings-locked .control-card {
            opacity: 0.45;
            filter: grayscale(0.6);
        }
        #page-settings {
            position: relative;
        }
        .settings-lock-overlay {
            display: none;
            position: absolute;
            inset: 20px;
            z-index: 60;
            align-items: center;
            justify-content: center;
            pointer-events: auto;
            background: rgba(15, 23, 42, 0.22);
        }
        .settings-locked .settings-lock-overlay {
            display: flex;
        }
        .settings-lock-panel {
            display: none;
        }
        #settings-auth-bar {
            position: relative;
            z-index: 80;
            width: min(860px, calc(100vw - 48px));
            margin: 0 auto 16px auto;
        }
        .settings-locked #settings-auth-bar {
            position: absolute;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
            margin: 0;
            cursor: grab;
            box-shadow: 0 18px 40px rgba(0, 0, 0, 0.38);
        }
        #settings-auth-bar.dragging {
            cursor: grabbing;
        }
        #records-display-table,
        #query-results-table {
            width: 100%;
            table-layout: fixed;
        }
        #records-display-table th,
        #records-display-table td,
        #query-results-table th,
        #query-results-table td {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .records-header-cell {
            position: relative;
            user-select: none;
            padding-right: 10px;
        }
        .record-col-resizer {
            position: absolute;
            top: 0;
            right: -3px;
            width: 8px;
            height: 100%;
            cursor: col-resize;
            touch-action: none;
            z-index: 2;
        }
        .record-col-resizer::after {
            content: "";
            position: absolute;
            top: 20%;
            bottom: 20%;
            left: 50%;
            width: 1px;
            background: rgba(148, 163, 184, 0.7);
            transform: translateX(-50%);
            opacity: 0;
            transition: opacity 0.15s ease;
        }
        .records-header-cell:hover .record-col-resizer::after,
        body.records-col-resizing .record-col-resizer::after {
            opacity: 1;
        }
        body.records-col-resizing {
            cursor: col-resize;
            user-select: none;
        }
        #plc-settings-card,
        #page-manual,
        #tab-manual {
            display: none !important;
        }

        /* Deterministic workstation layout for the test page. */
        #canvas::before {
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            background:
                radial-gradient(circle at 18% 12%, rgba(59, 130, 246, 0.08), transparent 28%),
                radial-gradient(circle at 84% 36%, rgba(14, 165, 233, 0.06), transparent 30%);
        }
        #page-test .control-card {
            background: linear-gradient(145deg, rgba(31, 43, 61, 0.96), rgba(24, 35, 51, 0.96));
            border-color: rgba(100, 116, 139, 0.48);
            box-shadow: 0 16px 36px rgba(2, 8, 23, 0.24);
        }
        #page-test .drag-handle {
            background: linear-gradient(90deg, rgba(51, 65, 85, 0.82), rgba(45, 58, 77, 0.62));
            min-height: 52px;
            display: flex;
            align-items: center;
        }
        #page-test .drag-handle > * {
            width: 100%;
        }
        #page-test .resize-handle {
            opacity: 0.2;
        }
        #chart-card > .p-4 {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
            padding: 14px;
        }
        #chart-card > .p-4 > div {
            min-width: 0;
        }
        #record-card > .p-4 > .overflow-auto {
            max-height: 224px !important;
        }
        @media (max-width: 900px) {
            #chart-card > .p-4 {
                grid-template-columns: 1fr;
            }
        }

        /* 1280x1024 monitor with a normally maximized browser (~1280x900 viewport). */
        @media (min-width: 1180px) {
            #page-test {
                overflow: hidden !important;
            }
            #canvas {
                display: grid;
                grid-template-columns: minmax(0, 0.95fr) minmax(0, 0.95fr) minmax(0, 1.3fr) minmax(0, 1.5fr);
                grid-template-rows: 210px 210px minmax(220px, 1fr);
                gap: 8px;
                width: 100% !important;
                height: 100% !important;
                min-height: 0 !important;
                padding: 10px;
                overflow: hidden !important;
                align-content: stretch;
            }
            #canvas > .control-card {
                position: relative !important;
                inset: auto !important;
                width: auto !important;
                height: auto !important;
                min-width: 0 !important;
                min-height: 0 !important;
                margin: 0 !important;
            }
            #product-select-card {
                grid-column: 1;
                grid-row: 1 / 3;
            }
            #monitor-card {
                grid-column: 2;
                grid-row: 1;
            }
            #control-card {
                grid-column: 2;
                grid-row: 2;
            }
            #status-lights-card {
                grid-column: 3;
                grid-row: 1 / 3;
            }
            #chart-card {
                grid-column: 4;
                grid-row: 1 / 3;
            }
            #record-card {
                grid-column: 1 / 5;
                grid-row: 3;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            #page-test .drag-handle {
                min-height: 44px;
                padding: 7px 10px !important;
                cursor: default;
            }
            #page-test .resize-handle {
                display: none;
            }
            #chart-card > .p-4 {
                display: grid;
                grid-template-columns: 1fr;
                gap: 8px;
                padding: 10px;
            }
            #chart-card > .p-4 > div {
                padding: 8px !important;
            }
            #chart-card > .p-4 > div > .mb-2 {
                margin-bottom: 4px !important;
            }
            #monitor-card > .p-4 {
                padding: 10px !important;
            }
            #monitor-card > .p-4.space-y-4 > :not([hidden]) ~ :not([hidden]) {
                margin-top: 6px !important;
            }
            #monitor-card > .p-4 > [class~="bg-gray-700/30"] {
                padding: 4px 10px !important;
            }
            #monitor-card > .p-4 > .text-center {
                line-height: 14px;
            }
            #status-lights-card > .p-4 {
                padding: 8px 10px !important;
            }
            #status-lights-card > .p-4.space-y-3 > :not([hidden]) ~ :not([hidden]) {
                margin-top: 5px !important;
            }
            #status-lights-card > .p-4 > .flex.gap-2 > div {
                padding: 4px 7px !important;
                border-radius: 10px !important;
            }
            #status-lights-card > .p-4 > .flex.gap-2 > div > div:first-child {
                width: 20px !important;
                height: 20px !important;
            }
            #status-lights-card > .p-4 > .space-y-2 > div {
                padding: 4px 8px !important;
                border-radius: 10px !important;
            }
            #status-lights-card > .p-4 > .space-y-2 > :not([hidden]) ~ :not([hidden]),
            #status-lights-card > .p-4 > div:last-child.space-y-2 > :not([hidden]) ~ :not([hidden]) {
                margin-top: 5px !important;
            }
            #status-lights-card > .p-4 > .space-y-2 .mb-2 {
                margin-bottom: 2px !important;
            }
            #status-lights-card > .p-4 > div:last-child {
                padding: 6px 8px !important;
                border-radius: 10px !important;
            }
            #status-lights-card > .p-4 > div:last-child select,
            #status-lights-card > .p-4 > div:last-child input,
            #status-lights-card > .p-4 > div:last-child button {
                height: 28px;
                padding-top: 2px !important;
                padding-bottom: 2px !important;
            }
            #record-card > .p-4 {
                display: flex;
                flex: 1 1 auto;
                min-height: 0;
                padding: 10px 12px !important;
            }
            #record-card > .p-4 > .overflow-auto {
                flex: 1 1 auto;
                min-height: 0;
                max-height: none !important;
            }
        }

        /* Settings workspace: full-width auth row with two aligned panels. */
        @media (min-width: 1180px) {
            #page-settings.tab-pane.active {
                display: grid;
                grid-template-columns: minmax(0, 1fr) 390px;
                grid-template-rows: 64px minmax(0, 1fr);
                gap: 12px;
                height: calc(100vh - 60px) !important;
                padding: 14px !important;
                overflow: hidden !important;
            }
            #settings-auth-bar {
                position: relative !important;
                grid-column: 1 / 3;
                grid-row: 1;
                left: auto !important;
                top: auto !important;
                width: 100% !important;
                max-width: none !important;
                min-width: 0;
                height: 64px;
                margin: 0 !important;
                transform: none !important;
                cursor: default !important;
                box-shadow: 0 12px 30px rgba(2, 8, 23, 0.22);
            }
            #page-settings.settings-locked #settings-auth-bar {
                position: relative !important;
                left: auto !important;
                top: auto !important;
                transform: none !important;
            }
            #page-settings > .control-card {
                position: relative !important;
                inset: auto !important;
                width: auto !important;
                height: auto !important;
                min-width: 0 !important;
                min-height: 0 !important;
                margin: 0 !important;
                background: linear-gradient(145deg, rgba(31, 43, 61, 0.96), rgba(24, 35, 51, 0.96));
                border-color: rgba(100, 116, 139, 0.48);
                box-shadow: 0 16px 36px rgba(2, 8, 23, 0.24);
            }
            #product-settings-card {
                grid-column: 1;
                grid-row: 2;
                display: flex;
                flex-direction: column;
            }
            #operator-settings-card {
                grid-column: 2;
                grid-row: 2;
                display: flex;
                flex-direction: column;
            }
            #page-settings .drag-handle {
                min-height: 44px;
                padding: 9px 14px !important;
                cursor: default !important;
            }
            #page-settings .resize-handle {
                display: none;
            }
            #product-settings-card > .p-4 {
                display: flex;
                flex: 1;
                min-height: 0;
                flex-direction: column;
                padding: 12px !important;
            }
            #product-settings-card > .p-4 > .overflow-x-auto {
                flex: 1;
                min-height: 0;
                overflow: auto;
            }
            #product-settings-card > .p-4 > .mt-4 {
                flex: none;
                margin-top: 10px !important;
            }
            #operator-list {
                flex: 1;
                min-height: 0;
                overflow-y: auto;
                padding: 12px !important;
            }
            #operator-list.space-y-2 > :not([hidden]) ~ :not([hidden]) {
                margin-top: 7px !important;
            }
            #operator-list input {
                height: 30px;
            }
        }
    </style>
</head>
<body>
    <!-- 标签页导航 -->
    <div class="tab-navigation fixed top-0 left-0 right-0 bg-gray-900/90 backdrop-blur z-50 border-b border-gray-700">
        <div class="max-w-full mx-auto px-4">
            <div class="flex space-x-1">
                <button id="tab-test" class="tab-btn active py-3 px-6 text-sm font-medium text-white border-b-2 border-blue-500">测试</button>
                <button id="tab-manual" class="tab-btn py-3 px-6 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent" style="display:none;">手动操作</button>
                <button id="tab-query" class="tab-btn py-3 px-6 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent">全量查询</button>
                <button id="tab-settings" class="tab-btn py-3 px-6 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent">设置</button>
            </div>
        </div>
    </div>
    
    <!-- 标签页内容 -->
    <div class="tab-content" style="padding-top: 60px; height: 100vh;">
        <!-- 测试页面 -->
        <div id="page-test" class="tab-pane active" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow-y: auto; overflow-x: hidden;">
        <div id="canvas" style="position: relative; width: 100%; min-height: 920px; overflow: visible;">
        <div id="control-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 390px; top: 245px; width: 310px; height: 185px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <h1 class="text-white font-semibold">ATEQ 控制</h1>
            </div>
            
            <div class="p-4 space-y-3">
                <div class="ateq-button-row">
                    <button id="btn-start" class="btn ateq-action-btn btn-start rounded-xl text-white font-semibold flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                        </svg>
                        启动
                    </button>
                    
                    <button id="btn-stop" class="btn ateq-action-btn btn-stop rounded-xl text-white font-semibold flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z"/>
                        </svg>
                        停止
                    </button>
                </div>
                
                <!-- 第二行按钮：扫码启动和打印标签 -->
                <div class="ateq-button-row">
                    <button id="btn-scan-start" class="btn ateq-action-btn scan-toggle-btn rounded-xl text-white" type="button" aria-pressed="false">
                        <span class="scan-toggle-icon-wrap" aria-hidden="true">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 00-1-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
                            </svg>
                        </span>
                        <span class="scan-toggle-copy">
                            <span class="scan-toggle-title">扫码启动</span>
                            <span id="scan-toggle-label" class="scan-toggle-state">已关闭</span>
                        </span>
                        <span class="scan-toggle-switch" aria-hidden="true">
                            <span class="scan-toggle-thumb"></span>
                        </span>
                    </button>
                    
                    <button id="btn-print-label" class="btn ateq-action-btn rounded-xl text-white font-semibold flex items-center justify-center gap-2 bg-teal-600 hover:bg-teal-500 transition-colors">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z"/>
                        </svg>
                        打印标签
                    </button>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        
        <!-- 产品选择卡片 -->
        <div id="product-select-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 50px; top: 50px; width: 320px; height: 500px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">产品选择</h1>
                    <div id="product-sync-status" class="text-xs text-gray-400">已同步</div>
                </div>
            </div>
            
            <div class="p-4 space-y-3">
                <!-- 产品型号选择 -->
                <div>
                    <label class="block text-sm text-gray-400 mb-1">产品型号</label>
                    <select id="product-selector" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-white text-sm">
                        <option value="">-- 请选择产品 --</option>
                    </select>
                </div>

                <div class="grid grid-cols-2 gap-2 text-xs">
                    <label class="flex items-center justify-between gap-2 bg-gray-700/30 rounded-lg px-3 py-2 text-gray-300">
                        <span>需要扫码</span>
                        <input id="switch-scan-required" type="checkbox" class="h-4 w-4 accent-blue-500">
                    </label>
                    <label class="flex items-center justify-between gap-2 bg-gray-700/30 rounded-lg px-3 py-2 text-gray-300">
                        <span>打印机</span>
                        <input id="switch-printer-enabled" type="checkbox" class="h-4 w-4 accent-blue-500">
                    </label>
                </div>

                <div id="ateq-communication-status-panel" class="bg-gray-700/30 rounded-lg p-3 text-xs">
                    <div class="flex items-center justify-end mb-2">
                        <span id="ateq-connection-status" class="px-2 py-0.5 rounded bg-gray-600 text-gray-200">读取中</span>
                    </div>
                    <div class="flex items-center gap-2">
                        <span id="ateq-communication-dot" class="w-2.5 h-2.5 rounded-full bg-gray-500"></span>
                        <span id="ateq-stepcode-value" class="text-white font-semibold">StepCode --</span>
                        <span id="ateq-communication-source" class="text-gray-500">COM1 · Station 255</span>
                    </div>
                    <div id="ateq-communication-message" class="mt-2 text-gray-500 leading-snug">等待ATEQ通讯...</div>
                </div>
                
                <!-- 程序配置显示 -->
                <div id="program-config" class="bg-gray-700/30 rounded-lg p-3 hidden">
                    <div class="grid grid-cols-2 gap-2 text-xs">
                        <div>
                            <span class="text-gray-400">程序号1:</span>
                            <span id="display-program-1" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">程序号2:</span>
                            <span id="display-program-2" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">切换腔道:</span>
                            <span id="display-switch-chamber" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">标签模板路径:</span>
                            <span id="display-label-template" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">供应商:</span>
                            <span id="display-supplier-code" class="text-white ml-1">--</span>
                        </div>
                    </div>
                </div>
                
                <!-- 测试状态显示 -->
                <div id="test-status-container" class="bg-gray-700/30 rounded-lg p-2 mt-2 hidden">
                    <div class="flex items-center justify-between">
                        <span class="text-gray-400 text-xs">测试状态:</span>
                        <span id="test-status-display" class="text-xs text-gray-400">待机</span>
                    </div>
                </div>

                <!-- 加载状态 -->
                <div id="product-loading" class="text-center text-gray-500 text-xs hidden">
                    <svg class="animate-spin w-4 h-4 mx-auto mb-1" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    加载中...
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        
        <!-- 实时数据监控卡片 -->
        <div id="monitor-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 390px; top: 50px; width: 310px; height: 185px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">实时数据</h1>
                    <div id="monitor-status" class="w-2 h-2 rounded-full bg-gray-400"></div>
                </div>
            </div>
            
            <div class="p-4 space-y-4">
                <!-- 压力显示 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-1">
                        <span class="text-gray-400 text-sm">压力</span>
                        <svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
                        </svg>
                    </div>
                    <div class="flex items-baseline gap-1">
                        <span id="pressure-value" class="text-2xl font-bold text-white">--</span>
                        <span id="pressure-unit" class="text-sm text-gray-400">--</span>
                    </div>
                </div>
                
                <!-- 泄漏量显示 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-1">
                        <span class="text-gray-400 text-sm">泄漏量</span>
                        <svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/>
                        </svg>
                    </div>
                    <div class="flex items-baseline gap-1">
                        <span id="leak-value" class="text-2xl font-bold text-white">--</span>
                        <span id="leak-unit" class="text-sm text-gray-400">--</span>
                    </div>
                </div>
                
                <!-- 状态显示 -->
                <div class="text-center">
                    <span id="device-status" class="text-xs text-gray-500">未连接</span>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        
        <!-- 状态指示灯卡片 -->
        <div id="status-lights-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 720px; top: 50px; width: 380px; min-height: 380px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">状态指示灯</h1>
                    <div class="text-xs text-gray-400">实时状态</div>
                </div>
            </div>
            
            <div class="p-4 space-y-3">
                <!-- 第一行：合格标志/循环结束 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试件合格，通过检测">
                        <div id="light-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">合格标志</div>
                            <div id="status-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试循环结束">
                        <div id="light-cycle-end" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">循环结束</div>
                            <div id="status-cycle-end" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第二行：测试件不合格/参考件不合格 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试件检测不合格">
                        <div id="light-fail-test" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">测试件不合格</div>
                            <div id="status-fail-test" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="参考件检测不合格">
                        <div id="light-fail-ref" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">参考件不合格</div>
                            <div id="status-fail-ref" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第三行：程序号1合格/程序号2合格 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="程序号1测试合格">
                        <div id="light-program1-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">程序号1合格</div>
                            <div id="status-program1-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="程序号2测试合格">
                        <div id="light-program2-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">程序号2合格</div>
                            <div id="status-program2-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第四行：程序号1结果/程序号2结果 -->
                <div class="space-y-2">
                    <div class="bg-gray-700/30 rounded-xl p-3">
                        <div class="text-white text-sm font-medium mb-2">程序号1结果</div>
                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">压力:</span>
                                <span id="program1-pressure" class="text-green-400 font-medium">--</span>
                                <span id="program1-pressure-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">泄漏量:</span>
                                <span id="program1-leak" class="text-blue-400 font-medium">--</span>
                                <span id="program1-leak-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">Result:</span>
                                <span id="program1-result" class="text-yellow-400 font-medium">--</span>
                            </div>
                        </div>
                    </div>
                    <div class="bg-gray-700/30 rounded-xl p-3">
                        <div class="text-white text-sm font-medium mb-2">程序号2结果</div>
                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">压力:</span>
                                <span id="program2-pressure" class="text-green-400 font-medium">--</span>
                                <span id="program2-pressure-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">泄漏量:</span>
                                <span id="program2-leak" class="text-blue-400 font-medium">--</span>
                                <span id="program2-leak-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">Result:</span>
                                <span id="program2-result" class="text-yellow-400 font-medium">--</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 第五行：操作人员和二维码 -->
                <div class="bg-gray-700/30 rounded-xl p-3 space-y-2">
                    <div>
                        <label class="block text-gray-400 text-xs mb-1">操作人员</label>
                        <select id="operator-input" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm">
                            <option value="">-- 选择操作人员 --</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-gray-400 text-xs mb-1">二维码</label>
                        <div class="flex gap-1">
                            <input type="text" id="qr-input" class="flex-1 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-gray-300 text-sm" placeholder="请扫码或手动输入..." autocomplete="off">
                            <button id="btn-clear-qr" class="px-2 py-1 bg-gray-500 hover:bg-gray-400 text-white text-xs rounded transition-colors" title="清空二维码">清空</button>
                        </div>
                        <div id="scanner-status" class="mt-1 text-[11px] text-gray-500 leading-snug">键盘扫码: 等待扫码枪输入</div>
                    </div>
                </div>
            </div>

            <div class="resize-handle"></div>
        </div>
        
        <!-- 实时曲线卡片 -->
        <div id="chart-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 50px; top: 450px; width: 700px; height: 380px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <h1 class="text-white font-semibold">实时曲线</h1>
                        <span id="chart-status" class="text-xs px-2 py-0.5 rounded bg-gray-600 text-gray-300">待机</span>
                    </div>
                    <div class="flex items-center gap-4 text-xs text-gray-400">
                        <span>Fill: <span id="fill-time">--</span>s</span>
                        <span>Stab: <span id="stab-time">--</span>s</span>
                        <span>Test: <span id="test-time">--</span>s</span>
                    </div>
                </div>
            </div>
            
            <div class="p-4 space-y-4">
                <!-- 压力曲线 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-gray-300 text-sm font-medium">压力曲线</span>
                        <span class="text-xs text-gray-500">X轴: Fill+Stab+Test</span>
                    </div>
                    <div style="height: 140px;">
                        <canvas id="pressure-chart"></canvas>
                    </div>
                </div>
                
                <!-- 泄漏量曲线 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-gray-300 text-sm font-medium">泄漏量曲线</span>
                        <span class="text-xs text-gray-500">X轴: Test Time</span>
                    </div>
                    <div style="height: 140px;">
                        <canvas id="leak-chart"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        
        <!-- 测试记录卡片 -->
        <div id="record-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 800px; top: 50px; width: 900px; min-height: 20px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">测试记录</h1>
                    <div class="flex items-center gap-2">
                        <span id="today-count" class="text-xs px-2 py-0.5 rounded bg-blue-600 text-white">今日: 0</span>
                        <span id="pass-rate" class="text-xs px-2 py-0.5 rounded bg-green-600 text-white">合格率: 0%</span>
                        <button id="btn-refresh" class="text-xs bg-gray-600 hover:bg-gray-500 text-white px-2 py-0.5 rounded transition-colors">刷新</button>
                    </div>
                </div>
            </div>
            
            <div class="p-4">
                <div class="overflow-auto" style="max-height: 180px;">
                        <table id="records-display-table" class="w-full border-collapse" style="font-size: 10px;">
                            <colgroup id="records-table-colgroup">
                                <col style="width: 52px;">
                                <col style="width: 165px;">
                                <col style="width: 120px;">
                                <col style="width: 170px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 90px;">
                                <col style="width: 120px;">
                                <col style="width: 96px;">
                            </colgroup>
                            <thead class="bg-gray-600 text-gray-200 sticky top-0">
                                <tr>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">序号<span class="record-col-resizer" data-col-index="0"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Time<span class="record-col-resizer" data-col-index="1"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Serial No.<span class="record-col-resizer" data-col-index="2"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">QR Code<span class="record-col-resizer" data-col-index="3"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#1 Pressure<span class="record-col-resizer" data-col-index="4"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#1 Leak<span class="record-col-resizer" data-col-index="5"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#2 Pressure<span class="record-col-resizer" data-col-index="6"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#2 Leak<span class="record-col-resizer" data-col-index="7"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Result<span class="record-col-resizer" data-col-index="8"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Part No.<span class="record-col-resizer" data-col-index="9"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Staff<span class="record-col-resizer" data-col-index="10"></span></th>
                                </tr>
                            </thead>
                            <tbody id="records-table" class="text-gray-300">
                                <!-- 动态填充 -->
                            </tbody>
                        </table>
                    </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        </div>
        </div>
        
        <!-- 手动操作页面 -->
        <div id="page-manual" class="tab-pane" style="display:none; position: relative; width: 100vw; height: calc(100vh - 60px); overflow-y: auto; padding: 20px;">
            <div class="max-w-4xl mx-auto space-y-6">
                <!-- 自动/手动开关卡片 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700 p-6">
                    <h2 class="text-white font-semibold text-lg mb-4 flex items-center gap-2">
                        <svg class="w-6 h-6 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/>
                        </svg>
                        操作模式
                    </h2>
                    <div class="flex items-center justify-between bg-gray-700/50 rounded-xl p-4">
                        <div>
                            <div class="text-white font-medium">自动/手动切换</div>
                            <div class="text-gray-400 text-sm">M26.0 - 控制设备操作模式</div>
                        </div>
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" id="switch-auto-manual" class="sr-only peer" onchange="toggleAutoManual(this)">
                            <div class="w-14 h-7 bg-gray-600 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-800 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-[4px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-6 after:w-6 after:transition-all peer-checked:bg-blue-600"></div>
                            <span class="ml-3 text-sm font-medium text-gray-300" id="mode-text">自动模式</span>
                        </label>
                    </div>
                </div>
                
                <!-- PLC手动控制卡片 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700 p-6">
                    <h2 class="text-white font-semibold text-lg mb-4 flex items-center gap-2">
                        <svg class="w-6 h-6 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z"/>
                        </svg>
                        PLC手动控制
                        <span id="manual-status-badge" class="px-2 py-0.5 rounded text-xs bg-red-600 text-white">自动模式-不可操作</span>
                    </h2>
                    
                    <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
                        <!-- 屏蔽安全门 M26.1 -->
                        <button id="btn-m26-1" class="plc-manual-btn bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 text-white rounded-xl p-4 transition-all flex flex-col items-center gap-2" onclick="operatePLC('M26.1', this)" disabled>
                            <svg class="w-8 h-8 text-yellow-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/>
                            </svg>
                            <span class="font-medium">屏蔽安全门</span>
                            <span class="text-xs text-gray-400">M26.1</span>
                        </button>
                        
                        <!-- 手动移载 M26.2 -->
                        <button id="btn-m26-2" class="plc-manual-btn bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 text-white rounded-xl p-4 transition-all flex flex-col items-center gap-2" onclick="operatePLC('M26.2', this)" disabled>
                            <svg class="w-8 h-8 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4"/>
                            </svg>
                            <span class="font-medium">手动移载</span>
                            <span class="text-xs text-gray-400">M26.2</span>
                        </button>
                        
                        <!-- 手动封堵 M26.3 -->
                        <button id="btn-m26-3" class="plc-manual-btn bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 text-white rounded-xl p-4 transition-all flex flex-col items-center gap-2" onclick="operatePLC('M26.3', this)" disabled>
                            <svg class="w-8 h-8 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
                            </svg>
                            <span class="font-medium">手动封堵</span>
                            <span class="text-xs text-gray-400">M26.3</span>
                        </button>
                        
                        <!-- 手动盖章 M26.4 -->
                        <button id="btn-m26-4" class="plc-manual-btn bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 text-white rounded-xl p-4 transition-all flex flex-col items-center gap-2" onclick="operatePLC('M26.4', this)" disabled>
                            <svg class="w-8 h-8 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                            </svg>
                            <span class="font-medium">手动盖章</span>
                            <span class="text-xs text-gray-400">M26.4</span>
                        </button>
                        
                        <!-- 手动排气 M26.5 -->
                        <button id="btn-m26-5" class="plc-manual-btn bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50 text-white rounded-xl p-4 transition-all flex flex-col items-center gap-2" onclick="operatePLC('M26.5', this)" disabled>
                            <svg class="w-8 h-8 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 10l-2 1m0 0l-2-1m2 1v2.5M20 7l-2 1m2-1l-2-1m2 1v2.5M14 4l-2-1-2 1M4 7l2-1M4 7l2 1M4 7v2.5M12 21l-2-1m2 1l2-1m-2 1v-2.5M6 18l-2-1v-2.5M18 18l2-1v-2.5"/>
                            </svg>
                            <span class="font-medium">手动排气</span>
                            <span class="text-xs text-gray-400">M26.5</span>
                        </button>
                    </div>
                    
                    <div class="mt-4 p-3 bg-gray-700/30 rounded-lg">
                        <div class="text-gray-400 text-sm flex items-center gap-2">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                            </svg>
                            <span>提示：切换到手动模式后，才能操作PLC手动控制按钮</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 全量查询页面 -->
        <div id="page-query" class="tab-pane" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow: hidden; padding: 15px;">
            <div class="h-full flex flex-col">
                <!-- 查询条件 - 第一行 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-xl overflow-hidden border border-gray-700 p-4 mb-4 flex-shrink-0">
                    <div class="flex flex-wrap items-center gap-3">
                        <!-- 开始日期 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">开始:</label>
                            <input type="date" id="query-start-date" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-36">
                        </div>
                        
                        <!-- 结束日期 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">结束:</label>
                            <input type="date" id="query-end-date" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-36">
                        </div>
                        
                        <!-- 产品型号 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">产品:</label>
                            <select id="query-product-model" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32">
                                <option value="">全部</option>
                            </select>
                        </div>
                        
                        <!-- 测试结果 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">结果:</label>
                            <select id="query-result" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-24">
                                <option value="">全部</option>
                                <option value="PASS">PASS</option>
                                <option value="FAIL">FAIL</option>
                            </select>
                        </div>
                        
                        <!-- QR码 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">QR码:</label>
                            <input type="text" id="query-qr-code" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32" placeholder="QR码">
                        </div>
                        
                        <!-- 序列号 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">序列号:</label>
                            <input type="text" id="query-serial" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32" placeholder="序列号">
                        </div>
                        
                        <!-- 每页数量 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">每页:</label>
                            <select id="query-limit" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-20">
                                <option value="20">20</option>
                                <option value="50">50</option>
                                <option value="100">100</option>
                                <option value="200">200</option>
                            </select>
                        </div>
                        
                        <!-- 操作按钮 -->
                        <div class="flex gap-2 ml-auto">
                            <button id="btn-query-search" class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
                                </svg>
                                查询
                            </button>
                            <button id="btn-query-reset" class="px-4 py-1.5 bg-gray-600 hover:bg-gray-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                                </svg>
                                重置
                            </button>
                            <button id="btn-query-export" class="px-4 py-1.5 bg-green-600 hover:bg-green-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                导出
                            </button>
                        </div>
                    </div>
                </div>
                
                <!-- 统计信息 - 第二行 -->
                <div class="grid grid-cols-4 gap-3 mb-4 flex-shrink-0">
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">总记录数</span>
                        <span id="stat-total" class="text-xl font-bold text-white">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">PASS</span>
                        <span id="stat-pass" class="text-xl font-bold text-green-400">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">FAIL</span>
                        <span id="stat-fail" class="text-xl font-bold text-red-400">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">合格率</span>
                        <span id="stat-rate" class="text-xl font-bold text-blue-400">0%</span>
                    </div>
                </div>
                
                <!-- 查询结果表格 - 占70%高度 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-xl overflow-hidden border border-gray-700 flex-1 flex flex-col" style="min-height: 0;">
                    <div class="p-3 border-b border-gray-700 flex justify-between items-center flex-shrink-0">
                        <span class="text-white font-semibold">查询结果</span>
                        <span id="query-page-info" class="text-gray-400 text-sm">第 1 页 / 共 0 页</span>
                    </div>
                    
                    <div class="flex-1 overflow-auto" style="min-height: 0;">
                        <table id="query-results-table" class="w-full text-xs">
                            <colgroup id="query-table-colgroup">
                                <col style="width: 56px;">
                                <col style="width: 168px;">
                                <col style="width: 180px;">
                                <col style="width: 120px;">
                                <col style="width: 120px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 90px;">
                                <col style="width: 96px;">
                            </colgroup>
                            <thead class="bg-gray-700 text-gray-200 sticky top-0">
                                <tr>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">序号<span class="record-col-resizer" data-query-col-index="0"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">测试时间<span class="record-col-resizer" data-query-col-index="1"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">QR码<span class="record-col-resizer" data-query-col-index="2"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">序列号<span class="record-col-resizer" data-query-col-index="3"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">产品型号<span class="record-col-resizer" data-query-col-index="4"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">压力1<span class="record-col-resizer" data-query-col-index="5"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">泄漏1<span class="record-col-resizer" data-query-col-index="6"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">压力2<span class="record-col-resizer" data-query-col-index="7"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">泄漏2<span class="record-col-resizer" data-query-col-index="8"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">结果<span class="record-col-resizer" data-query-col-index="9"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">操作员<span class="record-col-resizer" data-query-col-index="10"></span></th>
                                </tr>
                            </thead>
                            <tbody id="query-table-body" class="text-gray-300">
                                <tr>
                                    <td colspan="11" class="text-center py-8 text-gray-500">请执行查询</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- 分页 -->
                    <div class="p-3 border-t border-gray-700 flex justify-between items-center flex-shrink-0">
                        <div class="flex gap-2">
                            <button id="btn-page-first" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">首页</button>
                            <button id="btn-page-prev" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">上一页</button>
                            <button id="btn-page-next" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">下一页</button>
                            <button id="btn-page-last" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">末页</button>
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="text-gray-400 text-sm">跳转到</span>
                            <input type="number" id="page-input" class="w-16 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-white text-center" min="1" value="1">
                            <button id="btn-page-go" class="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors">GO</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 设置页面 -->
        <div id="page-settings" class="tab-pane" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow-y: auto; padding: 20px;">
            <div id="settings-auth-bar" class="mb-4 flex items-center justify-between gap-3 bg-gray-800/80 backdrop-blur rounded-2xl border border-gray-700 px-4 py-3">
                <div class="flex items-center gap-3">
                    <span class="text-sm text-gray-300">设置登录</span>
                    <span id="settings-login-status-badge" class="px-2 py-0.5 rounded text-xs bg-red-600 text-white">未登录</span>
                    <span id="settings-login-user-hint" class="text-xs text-gray-400">登录后可修改设置</span>
                </div>
                <div class="flex items-center gap-2">
                    <select id="settings-login-user" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[180px]">
                        <option value="">-- 选择用户名 --</option>
                    </select>
                    <input type="password" id="settings-login-password" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[160px]" placeholder="密码">
                    <button id="btn-settings-login" class="px-3 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm transition-colors">登录</button>
                    <button id="btn-settings-logout" class="px-3 py-2 bg-gray-600 hover:bg-gray-500 text-white rounded text-sm transition-colors" disabled>退出</button>
                </div>
            </div>
            <!-- 产品设置卡片 -->
            <div id="product-settings-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
                 style="left: 20px; top: 20px; width: 33.33%; min-height: 400px;">
                <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600 flex justify-between items-center cursor-grab">
                    <h2 class="text-white font-semibold text-xs">产品设置</h2>
                    <button id="btn-add-product" class="px-3 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg transition-colors flex items-center gap-2 text-xs">
                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
                        </svg>
                        添加产品
                    </button>
                </div>

                <div class="p-4">
                    <div class="overflow-x-auto">
                        <table class="w-full text-xs" id="product-table">
                            <thead class="bg-gray-600 text-gray-200">
                                <tr>
                                    <th class="text-center py-1 px-1 border border-gray-500 font-medium w-12">序号</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[120px]">产品型号</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">ATEQ程序号1</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">ATEQ程序号2</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">切换腔道</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[260px]">标签模板路径</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[120px]">供应商代码</th>
                                    <th class="text-center py-1 px-1 border border-gray-500 font-medium w-20">操作</th>
                                </tr>
                            </thead>
                            <tbody class="text-gray-300" id="product-tbody">
                            </tbody>
                        </table>
                    </div>
                    
                    <div class="mt-4 flex justify-end gap-3">
                        <button id="btn-save-settings" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg transition-colors text-xs">
                            保存设置
                        </button>
                    </div>
                </div>
                <div class="resize-handle"></div>
            </div>

            <!-- 操作人员设置卡片 -->
            <div id="operator-settings-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700"
                 style="left: 20px; top: 440px; width: 280px; min-height: 200px;">
                <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                    <h2 class="text-white font-semibold text-xs">操作人员</h2>
                </div>
                <div class="p-4 space-y-2" id="operator-list">
                </div>
                <div class="resize-handle"></div>
            </div>

            <!-- PLC设置卡片 -->
            <div id="plc-settings-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
                 style="display:none; left: 20px; top: 440px; width: 33.33%; min-height: 200px;">
                <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600 flex justify-between items-center cursor-grab">
                    <h2 class="text-white font-semibold text-xs">PLC设置</h2>
                </div>
                
                <div class="p-4 space-y-3">
                    <div>
                        <label class="block text-gray-400 text-xs mb-1">PLC IP地址</label>
                        <input type="text" id="plc-ip" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded text-white text-sm" placeholder="192.168.2.1" value="192.168.2.1">
                    </div>
                    <div>
                        <label class="block text-gray-400 text-xs mb-1">机架号 (Rack)</label>
                        <input type="number" id="plc-rack" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded text-white text-sm" placeholder="0" value="0">
                    </div>
                    <div>
                        <label class="block text-gray-400 text-xs mb-1">槽号 (Slot)</label>
                        <input type="number" id="plc-slot" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded text-white text-sm" placeholder="1" value="1">
                    </div>
                    <div class="flex gap-2">
                        <button id="btn-test-plc" class="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg transition-colors text-xs">
                            测试连接
                        </button>
                        <button id="btn-save-plc" class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg transition-colors text-xs">
                            保存设置
                        </button>
                    </div>
                    <div id="plc-connection-status" class="text-xs text-gray-400"></div>
                </div>
                <div class="resize-handle"></div>
            </div>
            <div id="settings-lock-overlay" class="settings-lock-overlay">
                <div class="settings-lock-panel">
                    <div class="text-amber-300 text-sm font-semibold">需要用户名登录</div>
                    <div class="mt-2 text-xs text-gray-300 leading-6">请先在测试页面选择用户名并登录，然后再修改设置。</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const CARD_LAYOUT_VERSION_KEY = 'ateq_layout_version';
        const CARD_LAYOUT_VERSION = '20260715_v8';
        const CARD_LAYOUT_STORAGE_KEYS = [
            'ateq_card',
            'ateq_plc_card',
            'monitor_card',
            'product_select_card',
            'chart_card',
            'record_card',
            'status_lights_card',
            'product_settings_card',
            'operator_settings_card'
        ];
        const CARD_LAYOUT_DEFAULTS = {
            'control-card': { storageKey: 'ateq_card', container: '#canvas', minWidth: 280, minHeight: 120, defaultLeft: 390, defaultTop: 245, defaultWidth: 310, defaultHeight: 185 },
            'monitor-card': { storageKey: 'monitor_card', container: '#canvas', minWidth: 250, minHeight: 150, defaultLeft: 390, defaultTop: 50, defaultWidth: 310, defaultHeight: 185 },
            'product-select-card': { storageKey: 'product_select_card', container: '#canvas', minWidth: 280, minHeight: 500, defaultLeft: 50, defaultTop: 50, defaultWidth: 320, defaultHeight: 500 },
            'status-lights-card': { storageKey: 'status_lights_card', container: '#canvas', minWidth: 320, minHeight: 280, defaultLeft: 720, defaultTop: 50, defaultWidth: 380, defaultHeight: 380 },
            'chart-card': { storageKey: 'chart_card', container: '#canvas', minWidth: 500, minHeight: 300, defaultLeft: 50, defaultTop: 450, defaultWidth: 700, defaultHeight: 380 },
            'record-card': { storageKey: 'record_card', container: '#canvas', minWidth: 350, minHeight: 220, defaultLeft: 800, defaultTop: 50, defaultWidth: 900, defaultHeight: 320 },
            'product-settings-card': { storageKey: 'product_settings_card', container: '#page-settings', minWidth: 360, minHeight: 320, defaultLeft: 20, defaultTop: 20, defaultWidth: 520, defaultHeight: 400 },
            'operator-settings-card': { storageKey: 'operator_settings_card', container: '#page-settings', minWidth: 280, minHeight: 200, defaultLeft: 20, defaultTop: 440, defaultWidth: 280, defaultHeight: 320 },
            'plc-settings-card': { storageKey: 'ateq_plc_card', container: '#page-settings', minWidth: 280, minHeight: 200, defaultLeft: 320, defaultTop: 440, defaultWidth: 420, defaultHeight: 240 }
        };

        function clampValue(value, min, max) {
            return Math.min(max, Math.max(min, value));
        }

        function migrateLegacyLayoutState() {
            try {
                if (localStorage.getItem(CARD_LAYOUT_VERSION_KEY) === CARD_LAYOUT_VERSION) {
                    return;
                }
                CARD_LAYOUT_STORAGE_KEYS.forEach(key => localStorage.removeItem(key));
                localStorage.setItem(CARD_LAYOUT_VERSION_KEY, CARD_LAYOUT_VERSION);
            } catch (error) {
                console.warn('布局缓存迁移失败:', error);
            }
        }

        function persistCardLayout(cardId) {
            const card = document.getElementById(cardId);
            const config = CARD_LAYOUT_DEFAULTS[cardId];
            if (!card || !config) return;

            try {
                localStorage.setItem(config.storageKey, JSON.stringify({
                    left: parseInt(card.style.left, 10) || config.defaultLeft,
                    top: parseInt(card.style.top, 10) || config.defaultTop,
                    width: parseInt(card.style.width, 10) || config.defaultWidth,
                    height: parseInt(card.style.minHeight, 10) || config.defaultHeight
                }));
            } catch (error) {
                console.warn('保存卡片布局失败:', cardId, error);
            }
        }

        function applyCardBox(cardId, left, top, width, height, fixedHeight = false) {
            const card = document.getElementById(cardId);
            if (!card) return;
            card.style.left = `${Math.round(left)}px`;
            card.style.top = `${Math.round(top)}px`;
            card.style.width = `${Math.round(width)}px`;
            card.style.minHeight = `${Math.round(height)}px`;
            card.style.height = fixedHeight ? `${Math.round(height)}px` : '';
            persistCardLayout(cardId);
        }

        function applyDesignedTestLayout() {
            const canvas = document.getElementById('canvas');
            if (!canvas) return;

            const margin = 22;
            const gap = 16;
            const width = canvas.clientWidth || window.innerWidth || 1366;

            if (width < 980) {
                const cardWidth = Math.max(320, width - margin * 2);
                let top = 18;
                applyCardBox('product-select-card', margin, top, cardWidth, 520, true);
                top += 536;
                applyCardBox('monitor-card', margin, top, cardWidth, 270, true);
                top += 286;
                applyCardBox('control-card', margin, top, cardWidth, 220, true);
                top += 236;
                applyCardBox('status-lights-card', margin, top, cardWidth, 590, true);
                top += 606;
                applyCardBox('chart-card', margin, top, cardWidth, 470, true);
                top += 486;
                applyCardBox('record-card', margin, top, cardWidth, 320, true);
                canvas.style.minHeight = `${top + 344}px`;
                return;
            }

            if (width < 1360) {
                const availableWidth = width - margin * 2 - gap;
                const leftWidth = Math.round(availableWidth * 0.46);
                const rightWidth = availableWidth - leftWidth;
                const left = margin;
                const right = left + leftWidth + gap;
                const top = 18;

                applyCardBox('product-select-card', left, top, leftWidth, 520, true);
                applyCardBox('monitor-card', right, top, rightWidth, 270, true);
                applyCardBox('control-card', right, top + 286, rightWidth, 234, true);

                const statusTop = top + 536;
                applyCardBox('status-lights-card', margin, statusTop, width - margin * 2, 590, true);
                const chartTop = statusTop + 606;
                applyCardBox('chart-card', margin, chartTop, width - margin * 2, 330, true);
                const recordTop = chartTop + 346;
                applyCardBox('record-card', margin, recordTop, width - margin * 2, 320, true);
                canvas.style.minHeight = `${recordTop + 344}px`;
                return;
            }

            const top = 18;
            const topHeight = 590;
            const availableTopWidth = width - margin * 2 - gap * 2;
            const productWidth = clampValue(Math.round(availableTopWidth * 0.27), 340, 430);
            const actionWidth = clampValue(Math.round(availableTopWidth * 0.24), 320, 390);
            const statusWidth = availableTopWidth - productWidth - actionWidth;
            const productLeft = margin;
            const actionLeft = productLeft + productWidth + gap;
            const statusLeft = actionLeft + actionWidth + gap;

            applyCardBox('product-select-card', productLeft, top, productWidth, topHeight, true);
            applyCardBox('monitor-card', actionLeft, top + 24, actionWidth, 270, true);
            applyCardBox('control-card', actionLeft, top + 310, actionWidth, 230, true);
            applyCardBox('status-lights-card', statusLeft, top, statusWidth, topHeight, true);

            const bottomTop = top + topHeight + gap;
            const availableBottomWidth = width - margin * 2 - gap;
            const chartWidth = Math.round(availableBottomWidth * 0.58);
            const recordWidth = availableBottomWidth - chartWidth;
            const bottomHeight = 320;
            applyCardBox('chart-card', margin, bottomTop, chartWidth, bottomHeight, true);
            applyCardBox('record-card', margin + chartWidth + gap, bottomTop, recordWidth, bottomHeight, true);

            canvas.style.minHeight = `${bottomTop + bottomHeight + 26}px`;
        }

        function applyDesignedSettingsLayout() {
            const page = document.getElementById('page-settings');
            if (!page) return;

            const margin = 20;
            const gap = 16;
            const width = page.clientWidth || window.innerWidth || 1366;
            const authHeight = document.getElementById('settings-auth-bar')?.offsetHeight || 64;

            if (width < 1180) {
                const cardWidth = Math.max(320, width - margin * 2);
                const productTop = authHeight + 36;
                applyCardBox('product-settings-card', margin, productTop, cardWidth, 440, true);
                applyCardBox('operator-settings-card', margin, productTop + 456, cardWidth, 500, true);
                applyCardBox('plc-settings-card', margin, productTop + 972, cardWidth, 260, true);
                page.style.minHeight = `${productTop + 1252}px`;
                return;
            }

            const productWidth = Math.min(620, Math.max(520, width - 760));
            const operatorWidth = 320;
            const top = authHeight + 36;

            applyCardBox('product-settings-card', margin, top, productWidth, 400);
            applyCardBox('operator-settings-card', margin, top + 420, operatorWidth, 420);
            applyCardBox('plc-settings-card', margin + operatorWidth + gap, top + 420, 420, 240);
        }

        function normalizeCardLayout(cardId) {
            const card = document.getElementById(cardId);
            const config = CARD_LAYOUT_DEFAULTS[cardId];
            if (!card || !config) return;

            const container = document.querySelector(config.container);
            if (!container || !container.offsetWidth || !container.offsetHeight) return;

            const measuredWidth = card.getBoundingClientRect().width || config.defaultWidth;
            const measuredHeight = card.getBoundingClientRect().height || config.defaultHeight;
            const maxWidth = Math.max(config.minWidth, container.clientWidth - 24);
            const maxHeight = Math.max(config.minHeight, container.clientHeight - 24);
            const width = clampValue(Math.max(measuredWidth, config.minWidth), config.minWidth, maxWidth);
            const height = clampValue(Math.max(measuredHeight, config.minHeight), config.minHeight, maxHeight);
            const rawLeft = parseFloat(card.style.left);
            const rawTop = parseFloat(card.style.top);
            const left = clampValue(Number.isFinite(rawLeft) ? rawLeft : config.defaultLeft, 0, Math.max(0, container.clientWidth - width - 12));
            const top = clampValue(Number.isFinite(rawTop) ? rawTop : config.defaultTop, 0, Math.max(0, container.clientHeight - height - 12));

            card.style.left = `${left}px`;
            card.style.top = `${top}px`;
            card.style.width = `${width}px`;
            card.style.minHeight = `${height}px`;

            if (cardId === 'chart-card') {
                if (typeof pressureChart !== 'undefined' && pressureChart) pressureChart.resize();
                if (typeof leakChart !== 'undefined' && leakChart) leakChart.resize();
            }
            if (cardId === 'record-card' && typeof applyRecordColumnWidths === 'function') {
                applyRecordColumnWidths();
            }

            persistCardLayout(cardId);
        }

        function normalizeVisibleCardLayouts(tabName) {
            const activeTab = tabName || document.querySelector('.tab-pane.active')?.id?.replace('page-', '') || 'test';
            if (activeTab === 'test') {
                applyDesignedTestLayout();
                if (typeof pressureChart !== 'undefined' && pressureChart) pressureChart.resize();
                if (typeof leakChart !== 'undefined' && leakChart) leakChart.resize();
                if (typeof applyRecordColumnWidths === 'function') applyRecordColumnWidths();
                return;
            }
            if (activeTab === 'settings') {
                applyDesignedSettingsLayout();
                return;
            }
            const layoutMap = {
                test: ['control-card', 'monitor-card', 'product-select-card', 'status-lights-card', 'chart-card', 'record-card'],
                settings: ['product-settings-card', 'operator-settings-card', 'plc-settings-card']
            };
            (layoutMap[activeTab] || []).forEach(cardId => normalizeCardLayout(cardId));
        }

        migrateLegacyLayoutState();
        window.addEventListener('resize', () => {
            window.requestAnimationFrame(() => normalizeVisibleCardLayouts());
        });

        function loadState() {
            const saved = localStorage.getItem('ateq_card');
            if (saved) {
                try {
                    const state = JSON.parse(saved);
                    const card = document.getElementById('control-card');
                    if (state.left) card.style.left = state.left + 'px';
                    if (state.top) card.style.top = state.top + 'px';
                    if (state.width) card.style.width = state.width + 'px';
                    if (state.height) card.style.minHeight = state.height + 'px';
                } catch(e) {}
            }
        }
        
        function saveState() {
            const card = document.getElementById('control-card');
            localStorage.setItem('ateq_card', JSON.stringify({
                left: parseInt(card.style.left) || 50,
                top: parseInt(card.style.top) || 50,
                width: parseInt(card.style.width) || 320,
                height: parseInt(card.style.minHeight) || 140
            }));
        }
        
        interact('#control-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { e.target.classList.remove('dragging'); saveState(); }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 120 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end() { saveState(); }
                }
            });
        
        // PLC设置卡片 - 拖拽和缩放功能
        function loadPlcState() {
            const saved = localStorage.getItem('ateq_plc_card');
            if (saved) {
                try {
                    const state = JSON.parse(saved);
                    const card = document.getElementById('plc-settings-card');
                    if (state.left) card.style.left = state.left + 'px';
                    if (state.top) card.style.top = state.top + 'px';
                    if (state.width) card.style.width = state.width + 'px';
                    if (state.height) card.style.minHeight = state.height + 'px';
                } catch(e) {}
            }
        }
        
        function savePlcState() {
            const card = document.getElementById('plc-settings-card');
            localStorage.setItem('ateq_plc_card', JSON.stringify({
                left: parseInt(card.style.left) || 20,
                top: parseInt(card.style.top) || 440,
                width: parseInt(card.style.width) || 320,
                height: parseInt(card.style.minHeight) || 200
            }));
        }
        
        interact('#plc-settings-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { e.target.classList.remove('dragging'); savePlcState(); }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 200 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end() { savePlcState(); }
                }
            });
        
        // 加载PLC卡片状态
        loadPlcState();
        
        document.getElementById('btn-start').addEventListener('click', async function() {
            const btn = this;
            const statusEl = document.getElementById('test-status-display');
            
            if (!currentProductConfig) {
                alert('请先选择产品型号');
                return;
            }
            
            const program1 = parseInt(currentProductConfig.program1) || 1;
            const program2 = parseInt(currentProductConfig.program2) || 0;
            const productModel = currentProductConfig.model || '';
            const labelTemplate = currentProductConfig.labelTemplate || '';
            const supplierCode = currentProductConfig.supplierCode || '';
            const operator = getActiveOperatorName();
            const qrCode = document.getElementById('qr-input')?.value || '';
            
            // 再次测试时清除程序号1和程序号2的结果
            clearProgramPassLights();
            
            btn.disabled = true;
            if (statusEl) {
                statusEl.textContent = '正在启动测试...';
                statusEl.className = 'text-xs text-yellow-400';
            }
            
            try {
                const params = new URLSearchParams({
                    program1: program1,
                    program2: program2,
                    switch_chamber: currentProductConfig.switchChamber === true,
                    product_model: productModel,
                    operator: operator,
                    qr_code: qrCode,
                    label_template: labelTemplate,
                    supplier_code: supplierCode
                });
                
                const response = await fetch(`/api/start_test_sequence?${params}`, { 
                    method: 'POST' 
                });
                const result = await response.json();
                
                if (result.success) {
                    
                    if (statusEl) {
                        statusEl.textContent = '测试已启动';
                        statusEl.className = 'text-xs text-green-400';
                    }
                    startStatusPolling();
                } else {
                    alert('启动失败: ' + result.message);
                    if (statusEl) {
                        statusEl.textContent = '启动失败';
                        statusEl.className = 'text-xs text-red-400';
                    }
                    btn.disabled = false;
                }
            } catch(e) {
                alert('启动异常: ' + e.message);
                if (statusEl) {
                    statusEl.textContent = '启动异常';
                    statusEl.className = 'text-xs text-red-400';
                }
                btn.disabled = false;
            }
        });
        
        document.getElementById('btn-stop').addEventListener('click', async function() {
            const btn = this;
            const statusEl = document.getElementById('test-status-display');
            
            btn.disabled = true;
            try {
                await fetch('/api/stop_test', { method: 'POST' });
                
                // 清除程序合格指示灯
                clearProgramPassLights();
                
                // 清空二维码输入框
                const qrInput = document.getElementById('qr-input');
                if (qrInput) {
                    qrInput.value = '';
                }
                
                if (statusEl) {
                    statusEl.textContent = '测试已停止';
                    statusEl.className = 'text-xs text-gray-400';
                }
                document.getElementById('btn-start').disabled = false;
            } catch(e) {
                console.error('停止失败:', e);
            }
            btn.disabled = false;
        });

        document.getElementById('btn-print-label').addEventListener('click', async function() {
            if (!currentProductConfig) {
                alert('请先选择产品型号');
                return;
            }

            const btn = this;
            btn.disabled = true;
            try {
                const response = await fetch('/api/print_label', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        product_model: currentProductConfig.model || '',
                        label_template: currentProductConfig.labelTemplate || '',
                        supplier_code: currentProductConfig.supplierCode || '',
                        qr_code: document.getElementById('qr-input')?.value || ''
                    })
                });
                const result = await response.json();
                if (result.success) {
                    showNotification('标签打印已发送：' + (result.sequence_code || ''), 'success');
                } else {
                    showNotification('打印失败: ' + (result.message || result.error || ''), 'error');
                }
            } catch (e) {
                showNotification('打印异常: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });

        const savedScanAutoStart = null;
        let scanAutoStartEnabled = false;

        function renderKeyboardScannerToggle() {
            const button = document.getElementById('btn-scan-start');
            const label = document.getElementById('scan-toggle-label');
            if (!button || !label) return;

            button.setAttribute('aria-pressed', scanAutoStartEnabled ? 'true' : 'false');
            button.disabled = true;
            label.textContent = 'StepCode/手动';
        }

        document.getElementById('btn-scan-start')?.addEventListener('click', function() {
            scanAutoStartEnabled = false;
            localStorage.setItem('scan_auto_start_enabled', String(scanAutoStartEnabled));
            renderKeyboardScannerToggle();
            const qrInput = document.getElementById('qr-input');
            if (qrInput) {
                qrInput.readOnly = false;
                qrInput.focus();
            }
            updateScannerStatus({ success: true, connected: true });
            showNotification(
                scanAutoStartEnabled
                    ? '扫码自动启动已开启，扫码后仪器会立即启动'
                    : '扫码后等待ATEQ StepCode由65535变为4，或点击“启动”',
                'info'
            );
        });

        document.getElementById('btn-clear-qr').addEventListener('click', function() {
            const qrInput = document.getElementById('qr-input');
            if (qrInput) {
                qrInput.value = '';
                qrInput.focus();
            }
        });

        let statusPollingInterval = null;
        let lastCompletionSequence = 0;
        let completionSequenceInitialized = false;
        let lastRecordSavedSequence = 0;
        let recordSavedSequenceInitialized = false;

        function startStatusPolling() {
            if (statusPollingInterval) return;

            statusPollingInterval = setInterval(async () => {
                try {
                    const response = await fetch('/api/test_status', { cache: 'no-store' });
                    const result = await response.json();

                    const statusEl = document.getElementById('test-status-display');
                    const btn = document.getElementById('btn-start');

                    if (result.success) {
                        const phase = result.phase || '';
                        const explicitSlot = Number(result.active_slot || result.current_program_slot || 0);
                        if (explicitSlot === 1 || explicitSlot === 2) {
                            activeProgramSlot = explicitSlot;
                        } else if (isProgram2Phase(phase)) {
                            activeProgramSlot = 2;
                        }
                        if (PROGRAM_CHART_PHASES.has(phase)) {
                            switchChartToProgramSlot(activeProgramSlot);
                        }
                        // 更新程序合格指示灯
                        updateProgramPassLights(result.program1_pass, result.program2_pass);

                        setProgramResultDisplay(
                            1,
                            result.program1_pressure,
                            result.program1_leak,
                            result.program1_result,
                            result.program1_pass,
                            result.program1_pressure_unit,
                            result.program1_leak_unit
                        );
                        setProgramResultDisplay(
                            2,
                            result.program2_pressure,
                            result.program2_leak,
                            result.program2_result,
                            result.program2_pass,
                            result.program2_pressure_unit,
                            result.program2_leak_unit
                        );

                        const recordSavedSequence = Number(result.record_saved_sequence || 0);
                        if (!recordSavedSequenceInitialized) {
                            lastRecordSavedSequence = recordSavedSequence;
                            recordSavedSequenceInitialized = true;
                            await loadRecords({ forceReload: true });
                        } else if (recordSavedSequence > lastRecordSavedSequence) {
                            lastRecordSavedSequence = recordSavedSequence;
                            await resetAfterSavedTest(result.saved_qr_code || '');
                            setTimeout(() => loadRecords({ forceReload: true }), RECORD_REFRESH_DELAY_MS);
                        }

                        const completionSequence = Number(result.completion_sequence || 0);
                        if (!completionSequenceInitialized) {
                            lastCompletionSequence = completionSequence;
                            // 页面首次载入只建立基线，避免旧的完成状态清掉刚扫入的新二维码。
                            lastCompletionSequence = completionSequence;
                            completionSequenceInitialized = true;
                        } else if (completionSequence > lastCompletionSequence) {
                            lastCompletionSequence = completionSequence;
                            // 使用稳定完成负载中的合格状态（比 test_data 更可靠）
                            const cp1Pass = result.program1_pass;
                            const cp2Pass = result.program2_pass;
                            updateProgramPassLights(cp1Pass, cp2Pass);
                            // 结果通知
                            if (result.overall_result) {
                                const isPass = result.overall_result === 'PASS';
                                if (isPass) {
                                    showNotification('测试合格: ' + (result.serial_number || ''), 'success');
                                } else {
                                    showNotification('测试不合格: ' + (result.overall_result || 'FAIL'), 'warning');
                                }
                            }
                            // 打印失败通知
                            if (result.print_attempted && !result.print_success) {
                                showNotification('打印失败: ' + (result.print_message || '未知错误'), 'warning');
                            }
                        }

                        if (!result.running) {
                            btn.disabled = false;
                            if (statusEl) {
                                const completionPhase = result.completion_phase || result.phase;
                                if (completionPhase === 'saved' || completionPhase === 'complete') {
                                    statusEl.textContent = '测试完成';
                                    statusEl.className = 'text-xs text-green-400';
                                } else if (completionPhase === 'save_error') {
                                    statusEl.textContent = '测试完成(记录保存失败)';
                                    statusEl.className = 'text-xs text-yellow-400';
                                } else if (completionPhase === 'error') {
                                    statusEl.textContent = '测试异常终止';
                                    statusEl.className = 'text-xs text-red-400';
                                } else {
                                    statusEl.textContent = '测试完成';
                                    statusEl.className = 'text-xs text-green-400';
                                }
                            }
                        } else if (statusEl) {
                            const phaseMessages = {
                                'reset': '正在重置设备...',
                                'select_program1': '选择程序1...',
                                'running_program1': '执行程序1测试...',
                                'program1_complete': '程序1完成',
                                'monitoring': '监控stepcode...',
                                'waiting': '等待启动程序2...',
                                'select_program2': '选择程序2...',
                                'running_program2': '执行程序2测试...',
                                'program2_complete': '程序2完成',
                                'complete': '测试完成',
                                'saved': '保存记录中...',
                                'save_error': '保存记录失败',
                                'error': '测试错误',
                                'stopped': '测试已停止'
                            };
                            statusEl.textContent = phaseMessages[result.phase] || result.phase || '测试中...';
                            statusEl.className = result.phase === 'error' || result.phase === 'save_error' ? 'text-xs text-red-400' : 'text-xs text-yellow-400';
                        }
                    }
                } catch(e) {
                    console.error('状态查询失败:', e);
                }
            }, 500);
        }
        
        function stopStatusPolling() {
            if (statusPollingInterval) {
                clearInterval(statusPollingInterval);
                statusPollingInterval = null;
            }
        }
        
        // 监控卡片拖拽
        interact('#monitor-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('monitor-card');
                        localStorage.setItem('monitor_card', JSON.stringify({
                            left: parseInt(card.style.left) || 400,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 300,
                            height: parseInt(card.style.minHeight) || 180
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 250, height: 150 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('monitor-card');
                        localStorage.setItem('monitor_card', JSON.stringify({
                            left: parseInt(card.style.left) || 400,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 300,
                            height: parseInt(card.style.minHeight) || 180
                        }));
                    }
                }
            });
        
        // 加载监控卡片位置
        const monitorSaved = localStorage.getItem('monitor_card');
        if (monitorSaved) {
            try {
                const state = JSON.parse(monitorSaved);
                const card = document.getElementById('monitor-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 产品选择卡片拖拽
        interact('#product-select-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('product-select-card');
                        localStorage.setItem('product_select_card', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 210,
                            width: parseInt(card.style.width) || 320,
                            height: Math.max(parseInt(card.style.minHeight) || 500, 500)
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 500 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('product-select-card');
                        localStorage.setItem('product_select_card', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 210,
                            width: parseInt(card.style.width) || 320,
                            height: Math.max(parseInt(card.style.minHeight) || 500, 500)
                        }));
                    }
                }
            });
        
        // 加载产品选择卡片位置
        const productSelectSaved = localStorage.getItem('product_select_card');
        if (productSelectSaved) {
            try {
                const state = JSON.parse(productSelectSaved);
                const card = document.getElementById('product-select-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = Math.max(parseInt(state.height) || 500, 500) + 'px';
            } catch(e) {}
        }
        
        // 当前选中的产品配置
        let currentProductConfig = null;
        let suppressProductAteqSyncOnce = false;
        const LOGIN_USER_STORAGE_KEY = 'ateq_logged_in_user';
        const OPERATOR_ACCOUNTS_STORAGE_KEY = 'ateq_operator_accounts';
        let loggedInUser = '';

        function readConfiguredAccounts() {
            try {
                const saved = localStorage.getItem(OPERATOR_ACCOUNTS_STORAGE_KEY);
                if (saved) {
                    const accounts = JSON.parse(saved);
                    if (Array.isArray(accounts)) {
                        return accounts
                            .map(item => ({
                                username: String(item?.username || '').trim(),
                                password: String(item?.password || '')
                            }))
                            .filter(item => item.username);
                    }
                }
            } catch (e) {}

            try {
                const legacy = localStorage.getItem('ateq_operators');
                const names = legacy ? JSON.parse(legacy) : [];
                if (Array.isArray(names)) {
                    return names
                        .map(name => ({ username: String(name || '').trim(), password: '' }))
                        .filter(item => item.username);
                }
            } catch (e) {}

            return [];
        }

        function readConfiguredOperators() {
            return readConfiguredAccounts().map(item => item.username).filter(Boolean);
        }

        function isSettingsLoginRequired() {
            return readConfiguredOperators().length > 0;
        }

        function canEditSettings() {
            return !isSettingsLoginRequired() || !!loggedInUser;
        }

        function getActiveOperatorName() {
            return String(document.getElementById('operator-input')?.value || '').trim();
        }

        function resetSettingsAuthBarPosition() {
            const authBar = document.getElementById('settings-auth-bar');
            if (!authBar) return;
            authBar.style.left = '';
            authBar.style.top = '';
            authBar.style.transform = '';
            authBar.removeAttribute('data-drag-free');
            authBar.classList.remove('dragging');
        }

        function applySettingsAccessState() {
            const locked = !canEditSettings();
            const settingsPage = document.getElementById('page-settings');

            if (settingsPage) {
                settingsPage.classList.toggle('settings-locked', locked);
                if (!locked) {
                    resetSettingsAuthBarPosition();
                }
            }

            document.querySelectorAll('#page-settings .control-card input, #page-settings .control-card select, #page-settings .control-card button, #page-settings .control-card textarea').forEach(control => {
                control.disabled = locked;
            });
        }

        function applyLoginUiState() {
            const select = document.getElementById('settings-login-user');
            const passwordInput = document.getElementById('settings-login-password');
            const loginButton = document.getElementById('btn-settings-login');
            const logoutButton = document.getElementById('btn-settings-logout');
            const badge = document.getElementById('settings-login-status-badge');
            const hint = document.getElementById('settings-login-user-hint');
            const hasUsers = isSettingsLoginRequired();
            const selectedUser = select ? String(select.value || '').trim() : '';

            if (badge) {
                if (loggedInUser) {
                    badge.textContent = '已登录';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] bg-green-600 text-white';
                } else if (hasUsers) {
                    badge.textContent = '未登录';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] bg-red-600 text-white';
                } else {
                    badge.textContent = '未配置';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] bg-yellow-600 text-white';
                }
            }

            if (hint) {
                if (loggedInUser) {
                    hint.textContent = '当前用户: ' + loggedInUser;
                } else if (hasUsers) {
                    hint.textContent = '登录后可修改设置';
                } else {
                    hint.textContent = '未配置用户名，可先进入设置维护';
                }
            }

            if (loginButton) {
                loginButton.disabled = !!loggedInUser || !selectedUser;
            }
            if (logoutButton) {
                logoutButton.disabled = !loggedInUser;
            }
            if (select) {
                select.disabled = !!loggedInUser || !hasUsers;
            }
            if (passwordInput) {
                passwordInput.disabled = !!loggedInUser || !hasUsers;
                if (loggedInUser) {
                    passwordInput.value = '';
                }
            }
        }

        function setLoggedInUser(userName) {
            loggedInUser = String(userName || '').trim();
            if (loggedInUser) {
                localStorage.setItem(LOGIN_USER_STORAGE_KEY, loggedInUser);
            } else {
                localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            }
            const passwordInput = document.getElementById('settings-login-password');
            if (passwordInput) {
                passwordInput.value = '';
            }

            applyLoginUiState();
            applySettingsAccessState();
        }

        function initializeLoginState() {
            const storedUser = String(localStorage.getItem(LOGIN_USER_STORAGE_KEY) || '').trim();
            const configuredUsers = readConfiguredOperators();

            if (configuredUsers.length === 0) {
                loggedInUser = '';
                localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            } else if (storedUser && !configuredUsers.includes(storedUser)) {
                loggedInUser = '';
                localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            } else {
                loggedInUser = storedUser;
            }

            applyLoginUiState();
            applySettingsAccessState();
        }

        function requireSettingsLogin() {
            if (canEditSettings()) {
                return true;
            }
            showNotification('请先在设置页面登录用户名', 'warning');
            return false;
        }

        async function loadLineSettings() {
            try {
                const response = await fetch('/api/line_settings');
                const data = await response.json();
                if (!data.success || !data.settings) return;
                const scanSwitch = document.getElementById('switch-scan-required');
                const printerSwitch = document.getElementById('switch-printer-enabled');
                if (scanSwitch) scanSwitch.checked = !!data.settings.scan_required;
                if (printerSwitch) printerSwitch.checked = !!data.settings.printer_enabled;
            } catch (e) {
                console.error('加载产线设置失败:', e);
            }
        }

        async function updateLineSettings(partial) {
            if (!requireSettingsLogin()) {
                await loadLineSettings();
                return;
            }
            try {
                const response = await fetch('/api/line_settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(partial)
                });
                const data = await response.json();
                if (!data.success) {
                    showNotification('设置切换失败: ' + (data.message || ''), 'error');
                    await loadLineSettings();
                    return;
                }
                await loadLineSettings();
            } catch (e) {
                showNotification('设置切换异常: ' + e.message, 'error');
                await loadLineSettings();
            }
        }

        document.getElementById('switch-scan-required')?.addEventListener('change', function() {
            updateLineSettings({ scan_required: this.checked });
        });

        document.getElementById('switch-printer-enabled')?.addEventListener('change', function() {
            updateLineSettings({ printer_enabled: this.checked });
        });

        document.getElementById('operator-input')?.addEventListener('change', function() {
            if (typeof syncTestContext === 'function') {
                syncTestContext();
            }
        });

        document.getElementById('settings-login-user')?.addEventListener('change', function() {
            applyLoginUiState();
        });

        document.getElementById('settings-login-password')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                document.getElementById('btn-settings-login')?.click();
            }
        });

        document.getElementById('btn-settings-login')?.addEventListener('click', function() {
            const userName = String(document.getElementById('settings-login-user')?.value || '').trim();
            const password = String(document.getElementById('settings-login-password')?.value || '');
            if (!userName) {
                showNotification('请先选择用户名', 'warning');
                return;
            }
            const account = readConfiguredAccounts().find(item => item.username === userName);
            if (!account) {
                showNotification('用户名不存在', 'error');
                return;
            }
            if (String(account.password || '') !== password) {
                showNotification('密码错误', 'error');
                return;
            }
            setLoggedInUser(userName);
            showNotification('已登录: ' + userName, 'success');
        });

        document.getElementById('btn-settings-logout')?.addEventListener('click', function() {
            const previousUser = loggedInUser;
            setLoggedInUser('');
            showNotification(previousUser ? ('已退出: ' + previousUser) : '已退出登录', 'info');
        });

        function updateM26StatusDisplay(data) {
            const panel = document.getElementById('plc-m26-status-panel');
            const status = document.getElementById('plc-connection-status');
            const dot = document.getElementById('plc-m26-dot');
            const value = document.getElementById('plc-m26-value');
            const source = document.getElementById('plc-m26-source');
            const message = document.getElementById('plc-m26-message');
            if (!panel || !status || !dot || !value || !source || !message) return;

            if (!data || !data.success || !data.connected) {
                status.textContent = 'PLC未连接';
                status.className = 'px-2 py-0.5 rounded bg-red-600 text-white';
                dot.className = 'w-2.5 h-2.5 rounded-full bg-red-500';
                value.textContent = '--';
                source.textContent = 'Snap7';
                message.textContent = (data && data.message) ? data.message : '无法读取M26.0 Snap7状态';
                message.className = 'mt-2 text-red-300 leading-snug';
                return;
            }

            const isOn = data.m26_0 === true;
            status.textContent = 'PLC已连接';
            status.className = 'px-2 py-0.5 rounded bg-green-600 text-white';
            dot.className = 'w-2.5 h-2.5 rounded-full ' + (isOn ? 'bg-green-400' : 'bg-gray-400');
            value.textContent = isOn ? 'ON' : 'OFF';
            value.className = 'font-semibold ' + (isOn ? 'text-green-300' : 'text-gray-200');
            source.textContent = (data.source === 'snap7' || data.source === 's7') ? 'Snap7' : (data.source === 'cache' ? 'OPC缓存' : (data.source || 'Snap7'));
            const ts = data.timestamp ? ('，时间: ' + data.timestamp) : '';
            const age = data.cache_age_seconds !== undefined ? ('，缓存龄=' + data.cache_age_seconds + '秒') : '';
            const prefix = data.stale ? '最近一次PLC读取正常，本次刷新失败' : (data.cached ? '上次PLC读取正常' : 'PLC读取正常');
            const detail = data.message ? ('；' + data.message) : '';
            message.textContent = prefix + '，质量=' + (data.quality ?? '') + ts + age + detail;
            message.className = 'mt-2 ' + (data.stale ? 'text-yellow-300' : 'text-gray-400') + ' leading-snug';
        }

        async function loadM26Status() {
            if (window.m26StatusLoading) return;
            window.m26StatusLoading = true;
            try {
                const response = await fetch('/api/plc/status');
                const data = await response.json();
                updateM26StatusDisplay(data);
            } catch (e) {
                updateM26StatusDisplay({ success: false, connected: false, message: e.message });
            } finally {
                window.m26StatusLoading = false;
            }
        }

        async function loadAteqCommunicationStatus() {
            const status = document.getElementById('ateq-connection-status');
            const dot = document.getElementById('ateq-communication-dot');
            const value = document.getElementById('ateq-stepcode-value');
            const source = document.getElementById('ateq-communication-source');
            const message = document.getElementById('ateq-communication-message');
            if (!status || !dot || !value || !source || !message) return;
            try {
                const response = await fetch('/api/data', { cache: 'no-store' });
                const data = await response.json();
                const connected = Boolean(data && data.success);
                status.textContent = connected ? 'ATEQ已连接' : 'ATEQ未连接';
                status.className = 'px-2 py-0.5 rounded text-white ' + (connected ? 'bg-green-600' : 'bg-red-600');
                dot.className = 'w-2.5 h-2.5 rounded-full ' + (connected ? 'bg-green-400' : 'bg-red-500');
                value.textContent = connected ? ('StepCode ' + data.step_code) : 'StepCode --';
                source.textContent = 'COM1 · Station 255';
                message.textContent = connected
                    ? (data.step_code === 65535 ? '待机已就绪；变为4时记录测试开始' : 'ATEQ通讯正常')
                    : ('通讯异常' + (data.error ? '：' + data.error : ''));
                message.className = 'mt-2 leading-snug ' + (connected ? 'text-gray-400' : 'text-red-300');
            } catch (error) {
                status.textContent = 'ATEQ未连接';
                status.className = 'px-2 py-0.5 rounded bg-red-600 text-white';
                dot.className = 'w-2.5 h-2.5 rounded-full bg-red-500';
                value.textContent = 'StepCode --';
                source.textContent = 'COM1 · Station 255';
                message.textContent = '通讯异常：' + error.message;
                message.className = 'mt-2 text-red-300 leading-snug';
            }
        }

        loadAteqCommunicationStatus();
        setInterval(loadAteqCommunicationStatus, 3000);

        let lastScannerSeq = Number(localStorage.getItem('last_scanner_seq') || '0');
        let scannerMarking = false;

        function updateScannerStatus(data, extraMessage = '') {
            const el = document.getElementById('scanner-status');
            if (!el) return;
            if (!data || !data.success) {
                el.textContent = '串口扫码: 异常 - ' + ((data && data.message) || '无法读取扫码状态');
                el.className = 'mt-1 text-[11px] text-red-300 leading-snug';
                return;
            }
            if (!data.connected) {
                el.textContent = '串口扫码: 未连接 ' + (data.port || 'COM4') + (data.error ? '，' + data.error : '，请关闭串口助手后重启Web UI');
                el.className = 'mt-1 text-[11px] text-red-300 leading-snug';
                return;
            }
            const codeText = data.code ? ('，最后: ' + data.code) : '，等待扫码';
            const timeText = data.timestamp ? ('，' + data.timestamp) : '';
            el.textContent = '串口扫码: 已连接 ' + data.port + ' ' + data.baudrate + codeText + timeText + extraMessage;
            el.className = 'mt-1 text-[11px] text-green-300 leading-snug';
        }

        updateScannerStatus = function(data, extraMessage = '') {
            const el = document.getElementById('scanner-status');
            if (!el) return;
            if (!data || !data.success) {
                el.textContent = '键盘扫码: 异常 - ' + ((data && data.message) || '无法读取扫码状态');
                el.className = 'mt-1 text-[11px] text-red-300 leading-snug';
                return;
            }
            const codeText = data.code ? ('，最近扫码: ' + data.code) : '，等待扫码枪输入';
            const timeText = data.timestamp ? ('，' + data.timestamp) : '';
            const modeText = scanAutoStartEnabled ? '，扫码后自动启动' : '，扫码后需点击启动';
            el.textContent = '键盘扫码: 已就绪' + modeText + codeText + timeText + extraMessage;
            el.className = 'mt-1 text-[11px] text-green-300 leading-snug';
        };

        async function markScanQualified(code) {
            if (scannerMarking || !code) return false;
            if (!currentProductConfig) {
                showNotification('请先选择产品型号，再进行扫码启动', 'warning');
                return false;
            }
            scannerMarking = true;
            try {
                const response = await fetch('/api/scan_qualified', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        qr_code: code,
                        program1: parseInt(currentProductConfig.program1) || 0,
                        program2: parseInt(currentProductConfig.program2) || 0,
                        switch_chamber: currentProductConfig.switchChamber === true,
                        product_model: currentProductConfig.model || '',
                        operator: getActiveOperatorName(),
                        label_template: currentProductConfig.labelTemplate || '',
                        supplier_code: currentProductConfig.supplierCode || ''
                    })
                });
                const result = await response.json();
                if (result.success) {
                    clearProgramPassLights();
                    const testStatus = document.getElementById('test-status-display');
                    if (testStatus) {
                        testStatus.textContent = '二维码已放行，等待启动';
                        testStatus.className = 'text-xs text-yellow-400';
                    }
                    updateScannerStatus({
                        success: true,
                        connected: true,
                        port: result.scanner_port || 'COM4',
                        baudrate: 9600,
                        code,
                        timestamp: ''
                    }, '，等待StepCode 65535→4，或点击“启动”');
                    loadAteqCommunicationStatus();
                    return true;
                } else {
                    showNotification('扫码启动失败: ' + (result.message || ''), 'error');
                    return false;
                }
            } catch (e) {
                showNotification('扫码确认异常: ' + e.message, 'error');
                return false;
            } finally {
                scannerMarking = false;
            }
        }

        async function pollSerialScanner() {
            try {
                const response = await fetch('/api/scanner/status');
                const data = await response.json();
                updateScannerStatus(data);
                if (!data.success || !data.connected || !data.code) return;

                const qrInput = document.getElementById("qr-input");
                if (qrInput && data.code && data.seq > lastScannerSeq) {
                    qrInput.value = '';
                    qrInput.value = data.code;
                    lastScannerSeq = data.seq;
                    localStorage.setItem('last_scanner_seq', String(data.seq));
                    await syncTestContext();
                    qrInput.focus();
                    const plcReady = await markScanQualified(data.code);
                    if (!plcReady) return;
                    if (scanAutoStartEnabled) {
                        showNotification("扫码成功，正在自动启动: " + data.code, "success");
                        document.getElementById('btn-start')?.click();
                    } else {
                        showNotification("扫码成功，等待StepCode 65535→4，或点击“启动”: " + data.code, "success");
                    }
                }
            } catch (e) {
                updateScannerStatus({ success: false, message: e.message });
            }
        }

        const KEYBOARD_SCAN_IDLE_MS = 120;
        const KEYBOARD_SCAN_MIN_LENGTH = 4;
        const KEYBOARD_SCAN_MAX_AVERAGE_KEY_MS = 80;
        let keyboardScanIdleTimer = null;
        let keyboardScanStartedAt = 0;
        let keyboardScanKeyCount = 0;
        let keyboardScanSubmitted = false;
        let lastSubmittedKeyboardCode = '';
        let lastSubmittedKeyboardAt = 0;

        async function resetAfterSavedTest(completedQrCode) {
            const qrInput = document.getElementById('qr-input');
            const currentCode = String(qrInput?.value || '').trim();
            const savedCode = String(completedQrCode || '').trim();

            // 下一件产品可能已被扫入，绝不能被上一周期的完成消息清掉。
            if (currentCode && (!savedCode || currentCode !== savedCode)) {
                console.log('保留已扫入的下一二维码:', currentCode);
                return false;
            }

            cancelKeyboardScanIdleTimer();
            keyboardScanStartedAt = 0;
            keyboardScanKeyCount = 0;
            keyboardScanSubmitted = false;
            lastKeyboardScannerInputAt = 0;
            lastSubmittedKeyboardCode = '';
            lastSubmittedKeyboardAt = 0;
            lastScannerSeq = 0;
            localStorage.removeItem('last_scanner_seq');

            if (qrInput) {
                qrInput.value = '';
                qrInput.readOnly = false;
                qrInput.focus();
            }
            await syncTestContext();
            updateScannerStatus({ success: true, connected: true }, '，上一记录已保存，等待下一个二维码');
            loadAteqCommunicationStatus();
            return true;
        }

        function cancelKeyboardScanIdleTimer() {
            if (keyboardScanIdleTimer !== null) {
                clearTimeout(keyboardScanIdleTimer);
                keyboardScanIdleTimer = null;
            }
        }

        async function handleKeyboardScan() {
            cancelKeyboardScanIdleTimer();
            const qrInput = document.getElementById('qr-input');
            const code = String(qrInput?.value || '').trim();
            if (!code) {
                updateScannerStatus({ success: true, connected: true });
                return;
            }
            const now = Date.now();
            if (code === lastSubmittedKeyboardCode && now - lastSubmittedKeyboardAt < 1000) {
                return;
            }
            lastSubmittedKeyboardCode = code;
            lastSubmittedKeyboardAt = now;
            keyboardScanSubmitted = true;
            if (qrInput) {
                qrInput.value = code;
                qrInput.focus();
            }
            await syncTestContext();
            const plcReady = await markScanQualified(code);
            if (!plcReady) return;
            if (scanAutoStartEnabled) {
                showNotification('扫码成功，正在自动启动: ' + code, 'success');
                document.getElementById('btn-start')?.click();
            } else {
                showNotification('扫码成功，等待StepCode 65535→4，或点击“启动”: ' + code, 'success');
            }
        }

        function scheduleKeyboardScanAutoSubmit() {
            cancelKeyboardScanIdleTimer();
            keyboardScanIdleTimer = setTimeout(() => {
                keyboardScanIdleTimer = null;
                const qrInput = document.getElementById('qr-input');
                const code = String(qrInput?.value || '').trim();
                const typingDuration = Math.max(0, lastKeyboardScannerInputAt - keyboardScanStartedAt);
                const averageKeyMs = keyboardScanKeyCount > 1
                    ? typingDuration / (keyboardScanKeyCount - 1)
                    : Number.POSITIVE_INFINITY;

                if (
                    code.length >= KEYBOARD_SCAN_MIN_LENGTH &&
                    keyboardScanKeyCount >= KEYBOARD_SCAN_MIN_LENGTH &&
                    averageKeyMs <= KEYBOARD_SCAN_MAX_AVERAGE_KEY_MS
                ) {
                    handleKeyboardScan();
                }
            }, KEYBOARD_SCAN_IDLE_MS);
        }

        let lastKeyboardScannerInputAt = 0;

        function isTestPageActive() {
            return document.getElementById('page-test')?.classList.contains('active');
        }

        function shouldCaptureKeyboardScanner(event) {
            if (!isTestPageActive()) return false;
            if (event.ctrlKey || event.altKey || event.metaKey) return false;
            return event.key === 'Enter' || event.key === 'Tab' || event.key.length === 1;
        }

        document.addEventListener('keydown', function(event) {
            if (!shouldCaptureKeyboardScanner(event)) return;

            const qrInput = document.getElementById('qr-input');
            if (!qrInput) return;

            event.preventDefault();
            event.stopPropagation();

            if (event.key === 'Enter' || event.key === 'Tab') {
                cancelKeyboardScanIdleTimer();
                handleKeyboardScan();
                return;
            }

            const now = Date.now();
            if (now - lastKeyboardScannerInputAt > 300) {
                keyboardScanStartedAt = now;
                keyboardScanKeyCount = 0;
                if (keyboardScanSubmitted || document.activeElement !== qrInput) {
                    qrInput.value = '';
                }
                keyboardScanSubmitted = false;
            }
            lastKeyboardScannerInputAt = now;
            keyboardScanKeyCount += 1;

            qrInput.readOnly = false;
            qrInput.value += event.key;
            qrInput.focus();
            syncTestContext();
            updateScannerStatus({ success: true, connected: true, code: qrInput.value });
            scheduleKeyboardScanAutoSubmit();
        }, true);

        renderKeyboardScannerToggle();
        const initialQrInput = document.getElementById('qr-input');
        if (initialQrInput) initialQrInput.readOnly = false;
        updateScannerStatus({ success: true, connected: true });
        
        // 加载产品列表到下拉菜单
        function loadProductSelector() {
            const saved = localStorage.getItem('ateq_products');
            const selector = document.getElementById('product-selector');
            const loading = document.getElementById('product-loading');
            const syncStatus = document.getElementById('product-sync-status');
            
            loading.classList.remove('hidden');
            syncStatus.textContent = '同步中...';
            syncStatus.className = 'text-xs text-yellow-400';
            
            selector.innerHTML = '<option value="">-- 请选择产品 --</option>';
            
            setTimeout(() => {
                try {
                    if (saved) {
                        const products = JSON.parse(saved);
                        if (products && products.length > 0) {
                            products.forEach((product, index) => {
                                if (product.model) {
                                    const option = document.createElement('option');
                                    option.value = index;
                                    option.textContent = product.model;
                                    option.dataset.program1 = product.program1 || '';
                                    option.dataset.program2 = product.program2 || '';
                                    option.dataset.switchChamber = product.switchChamber ? 'true' : 'false';
                                    option.dataset.labelTemplate = product.labelTemplate || '';
                                    option.dataset.supplierCode = product.supplierCode || '';
                                    selector.appendChild(option);
                                }
                            });
                            
                            syncStatus.textContent = '已同步 ' + products.length + ' 个产品';
                            syncStatus.className = 'text-xs text-green-400';
                        } else {
                            syncStatus.textContent = '无产品数据';
                            syncStatus.className = 'text-xs text-gray-400';
                        }
                    } else {
                        syncStatus.textContent = '未配置产品';
                        syncStatus.className = 'text-xs text-orange-400';
                    }
                } catch(e) {
                    console.error('加载产品列表失败:', e);
                    syncStatus.textContent = '加载失败';
                    syncStatus.className = 'text-xs text-red-400';
                }
                
                loading.classList.add('hidden');
                
                // 恢复上次选择的产品
                const lastSelected = localStorage.getItem('selected_product_index');
                if (lastSelected !== null) {
                    selector.value = lastSelected;
                    selector.dispatchEvent(new Event('change'));
                }
            }, 100);
        }
        
        // 产品选择变化事件
        document.getElementById('product-selector').addEventListener('change', function() {
            const selectedIndex = this.value;
            const suppressAteqSync = suppressProductAteqSyncOnce;
            suppressProductAteqSyncOnce = false;
            const configDiv = document.getElementById('program-config');
            const statusContainer = document.getElementById('test-status-container');

            if (selectedIndex === '') {
                configDiv.classList.add('hidden');
                if (statusContainer) statusContainer.classList.add('hidden');
                currentProductConfig = null;
                localStorage.removeItem('selected_product_index');
                return;
            }
            
            const selectedOption = this.options[this.selectedIndex];
            currentProductConfig = {
                model: selectedOption.textContent,
                program1: selectedOption.dataset.program1,
                program2: selectedOption.dataset.program2,
                switchChamber: selectedOption.dataset.switchChamber === 'true',
                labelTemplate: selectedOption.dataset.labelTemplate,
                supplierCode: selectedOption.dataset.supplierCode
            };
            
            // 显示配置信息
            document.getElementById('display-program-1').textContent = currentProductConfig.program1 || '--';
            document.getElementById('display-program-2').textContent = currentProductConfig.program2 || '--';
            document.getElementById('display-switch-chamber').textContent = currentProductConfig.switchChamber ? '是' : '否';
            document.getElementById('display-label-template').textContent = currentProductConfig.labelTemplate || '--';
            document.getElementById('display-supplier-code').textContent = currentProductConfig.supplierCode || '--';
            
            configDiv.classList.remove('hidden');
            if (statusContainer) statusContainer.classList.remove('hidden');

            // 保存选择
            localStorage.setItem('selected_product_index', selectedIndex);

            // 立刻写入程序号1到ATEQ仪器
            const prog1 = parseInt(currentProductConfig.program1);
            if (!suppressAteqSync && prog1 > 0) {
                fetch('/api/select_program/' + prog1, { method: 'POST' })
                    .then(r => r.json())
                    .then(r => { if (!r.success) console.warn('写入程序号1失败:', r.message); })
                    .catch(e => console.error('写入程序号1异常:', e))
                    .finally(() => loadTestParams(prog1));
            } else if (!suppressAteqSync) {
                loadTestParams();
            }

            // 同步测试上下文到后台（PLC硬件触发用）
            syncTestContext();

            // 时间参数在程序号写入完成后刷新，避免读取到上一个型号的参数

            // 清空曲线数据（新产品的测试参数可能不同）
            pressureData = [];
            leakData = [];
            testStartTime = null;
            testPhaseStartTime = null;
            lastStepCode = 0;
            if (pressureChart) { pressureChart.data.datasets[0].data = []; pressureChart.update('none'); }
            if (leakChart) { leakChart.data.datasets[0].data = []; leakChart.update('none'); }

            // 刷新测试记录列表
            loadRecords();

            // 清除之前的测试结果指示灯
            clearStatusLights();
        });
        
        // 页面加载时初始化产品选择器
        loadProductSelector();
        loadLineSettings();
        loadOperators();
        initializeLoginState();
        
        // 保存上一次的有效压力值
        let lastValidPressure = null;
        let lastValidPressureUnit = localStorage.getItem('ateqPressureUnit') || '--';
        let lastValidLeak = null;
        let lastValidLeakUnit = localStorage.getItem('ateqLeakUnit') || '--';
        let lastStepcode = 0;
        const FINAL_IDLE_CAPTURE_MS = 1200;
        const RECORD_REFRESH_DELAY_MS = 1400;
        let idleCaptureStartedAt = 0;
        let activeProgramSlot = 1;
        const PROGRAM2_PHASES = new Set(['select_program2', 'running_program2', 'program2_complete']);
        const PROGRAM_CHART_PHASES = new Set([
            'select_program1', 'running_program1',
            'select_program2', 'running_program2'
        ]);

        function isProgram2Phase(phase) {
            return PROGRAM2_PHASES.has(String(phase || ''));
        }

        function applyAteqUnits(pressureUnit, leakUnit, persist = false) {
            const nextPressureUnit = pressureUnit || lastValidPressureUnit || '--';
            const nextLeakUnit = leakUnit || lastValidLeakUnit || '--';
            const changed = nextPressureUnit !== lastValidPressureUnit || nextLeakUnit !== lastValidLeakUnit;

            lastValidPressureUnit = nextPressureUnit;
            lastValidLeakUnit = nextLeakUnit;
            if (persist) {
                localStorage.setItem('ateqPressureUnit', nextPressureUnit);
                localStorage.setItem('ateqLeakUnit', nextLeakUnit);
            }
            const pressureElement = document.getElementById('pressure-unit');
            const leakElement = document.getElementById('leak-unit');
            if (pressureElement) pressureElement.textContent = nextPressureUnit;
            if (leakElement) leakElement.textContent = nextLeakUnit;

            return changed;
        }

        applyAteqUnits(lastValidPressureUnit, lastValidLeakUnit);

        function setProgramResultDisplay(slot, pressure, leak, resultValue, passValue, pressureUnit, leakUnit) {
            const prefix = slot === 2 ? 'program2' : 'program1';
            if (pressure !== null && pressure !== undefined) {
                document.getElementById(`${prefix}-pressure`).textContent = Number(pressure).toFixed(3);
                document.getElementById(`${prefix}-pressure-unit`).textContent = pressureUnit || '--';
            }
            if (leak !== null && leak !== undefined) {
                document.getElementById(`${prefix}-leak`).textContent = Number(leak).toFixed(3);
                document.getElementById(`${prefix}-leak-unit`).textContent = leakUnit || '--';
            }
            if (resultValue !== null && resultValue !== undefined) {
                updateProgramResultText(`${prefix}-result`, resultValue, passValue);
            }
        }
        
        // 实时数据更新
        async function updateMonitorData() {
            try {
                const response = await fetch('/api/data', { cache: 'no-store' });
                const data = await response.json();
                
                if (data.success) {
                    if (data.step_code === 65535 && lastStepcode !== 65535) {
                        idleCaptureStartedAt = Date.now();
                    }

                    const idleCaptureActive =
                        data.step_code === 65535 &&
                        idleCaptureStartedAt > 0 &&
                        (Date.now() - idleCaptureStartedAt) <= FINAL_IDLE_CAPTURE_MS;

                    const capturePressureRealtime = data.step_code >= 4 && data.step_code <= 6;
                    const captureLeakRealtime = (data.step_code >= 4 && data.step_code <= 6) || idleCaptureActive;
                    const captureTestUnits = data.step_code >= 4 && data.step_code <= 6;
                    const unitsUninitialized = lastValidPressureUnit === '--' || lastValidLeakUnit === '--';
                    if (captureTestUnits || unitsUninitialized) {
                        const unitsChanged = applyAteqUnits(data.pressure_unit, data.leak_unit, captureTestUnits);
                        if (unitsChanged) {
                            loadRecords({ forceReload: true });
                        }
                    }

                    if (capturePressureRealtime) {
                        lastValidPressure = data.pressure;
                        document.getElementById('pressure-value').textContent = data.pressure.toFixed(3);
                        document.getElementById('pressure-unit').textContent = lastValidPressureUnit;
                    } else if (lastValidPressure !== null) {
                        document.getElementById('pressure-value').textContent = lastValidPressure.toFixed(3);
                        document.getElementById('pressure-unit').textContent = lastValidPressureUnit;
                    }

                    if (captureLeakRealtime) {
                        lastValidLeak = data.leak;
                        document.getElementById('leak-value').textContent = data.leak.toFixed(3);
                        document.getElementById('leak-unit').textContent = lastValidLeakUnit;
                    } else if (lastValidLeak !== null) {
                        document.getElementById('leak-value').textContent = lastValidLeak.toFixed(3);
                        document.getElementById('leak-unit').textContent = lastValidLeakUnit;
                    }

                    document.getElementById('device-status').textContent = data.status_text;
                    document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-green-400';

                    // stepcode跳变到4-100范围时，新测试周期开始，清除所有指示灯
                    if (data.step_code >= 4 && data.step_code <= 100 && lastStepcode !== data.step_code && (lastStepcode === 0 || lastStepcode === 65535 || lastStepcode < 4)) {
                        clearStatusLights();
                    }

                    lastStepcode = data.step_code;

                    // 更新状态指示灯
                    updateStatusLights(data.status);
                } else {
                    document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-red-400';
                    document.getElementById('device-status').textContent = '连接失败';
                }
            } catch(e) {
                document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-gray-400';
                document.getElementById('device-status').textContent = '未连接';
            }
        }
        
        // 更新状态指示灯
        function updateStatusLights(status) {
            // 状态字位定义（基于F5协议）
            // 位0: 合格标志
            // 位1: 测试件不合格
            // 位2: 参考件不合格
            // 位5: 循环结束
            
            const bitPass = (status & 0x0001) !== 0;
            const bitFailTest = (status & 0x0002) !== 0;
            const bitFailRef = (status & 0x0004) !== 0;
            const bitCycleEnd = (status & 0x0020) !== 0;
            
            // 更新合格标志指示灯（绿色）
            const lightPass = document.getElementById('light-pass');
            const statusPass = document.getElementById('status-pass');
            if (bitPass && bitCycleEnd) {
                lightPass.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusPass.textContent = '已激活';
                statusPass.className = 'text-xs text-green-400';
            } else {
                lightPass.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusPass.textContent = '未激活';
                statusPass.className = 'text-gray-500 text-xs';
            }
            
            // 更新测试件不合格指示灯（红色）
            const lightFailTest = document.getElementById('light-fail-test');
            const statusFailTest = document.getElementById('status-fail-test');
            if (bitFailTest && bitCycleEnd) {
                lightFailTest.className = 'w-6 h-6 rounded-full bg-red-500 shadow-lg shadow-red-500/50 transition-all duration-300';
                statusFailTest.textContent = '已激活';
                statusFailTest.className = 'text-xs text-red-400';
            } else {
                lightFailTest.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusFailTest.textContent = '未激活';
                statusFailTest.className = 'text-gray-500 text-xs';
            }
            
            // 更新参考件不合格指示灯（橙色）
            const lightFailRef = document.getElementById('light-fail-ref');
            const statusFailRef = document.getElementById('status-fail-ref');
            if (bitFailRef && bitCycleEnd) {
                lightFailRef.className = 'w-6 h-6 rounded-full bg-orange-500 shadow-lg shadow-orange-500/50 transition-all duration-300';
                statusFailRef.textContent = '已激活';
                statusFailRef.className = 'text-xs text-orange-400';
            } else {
                lightFailRef.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusFailRef.textContent = '未激活';
                statusFailRef.className = 'text-gray-500 text-xs';
            }
            
            // 更新循环结束标志指示灯（蓝色）
            const lightCycleEnd = document.getElementById('light-cycle-end');
            const statusCycleEnd = document.getElementById('status-cycle-end');
            if (bitCycleEnd) {
                lightCycleEnd.className = 'w-6 h-6 rounded-full bg-blue-500 shadow-lg shadow-blue-500/50 transition-all duration-300';
                statusCycleEnd.textContent = '已激活';
                statusCycleEnd.className = 'text-xs text-blue-400';
            } else {
                lightCycleEnd.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusCycleEnd.textContent = '未激活';
                statusCycleEnd.className = 'text-gray-500 text-xs';
            }
        }
        
        // 清除所有指示灯
        function clearStatusLights() {
            const lights = ['light-pass', 'light-fail-test', 'light-fail-ref', 'light-cycle-end'];
            const statuses = ['status-pass', 'status-fail-test', 'status-fail-ref', 'status-cycle-end'];
            
            lights.forEach((id, index) => {
                const light = document.getElementById(id);
                const status = document.getElementById(statuses[index]);
                if (light) {
                    light.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                }
                if (status) {
                    status.textContent = '未激活';
                    status.className = 'text-gray-500 text-xs';
                }
            });
            
            // 同时清除程序合格指示灯
            clearProgramPassLights();
        }
        
        // 更新程序合格指示灯
        function updateProgramPassLights(program1Pass, program2Pass) {
            // 更新程序号1合格指示灯（绿色）
            const lightProgram1 = document.getElementById('light-program1-pass');
            const statusProgram1 = document.getElementById('status-program1-pass');
            if (program1Pass) {
                lightProgram1.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusProgram1.textContent = '合格';
                statusProgram1.className = 'text-xs text-green-400';
            } else {
                lightProgram1.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusProgram1.textContent = '未激活';
                statusProgram1.className = 'text-gray-500 text-xs';
            }
            
            // 更新程序号2合格指示灯（绿色）
            const lightProgram2 = document.getElementById('light-program2-pass');
            const statusProgram2 = document.getElementById('status-program2-pass');
            if (program2Pass) {
                lightProgram2.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusProgram2.textContent = '合格';
                statusProgram2.className = 'text-xs text-green-400';
            } else {
                lightProgram2.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusProgram2.textContent = '未激活';
                statusProgram2.className = 'text-gray-500 text-xs';
            }
        }

        function updateProgramResultText(elementId, resultValue, passValue) {
            const el = document.getElementById(elementId);
            if (!el) {
                return;
            }

            if (resultValue === null || resultValue === undefined || resultValue === '') {
                el.textContent = '--';
                el.className = 'text-yellow-400 font-medium';
                return;
            }

            const normalized = String(resultValue).toUpperCase();
            el.textContent = normalized;

            if (normalized === 'PASS' || passValue === true) {
                el.className = 'text-green-400 font-medium';
            } else if (normalized === 'FAIL' || passValue === false) {
                el.className = 'text-red-400 font-medium';
            } else {
                el.className = 'text-yellow-400 font-medium';
            }
        }
        
        // 清除程序合格指示灯
        function clearProgramPassLights() {
            activeProgramSlot = 1;
            idleCaptureStartedAt = 0;

            const lights = ['light-program1-pass', 'light-program2-pass'];
            const statuses = ['status-program1-pass', 'status-program2-pass'];
            
            lights.forEach((id, index) => {
                const light = document.getElementById(id);
                const status = document.getElementById(statuses[index]);
                if (light) {
                    light.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                }
                if (status) {
                    status.textContent = '未激活';
                    status.className = 'text-gray-500 text-xs';
                }
            });
            
            // 清除程序结果数据
            document.getElementById('program1-pressure').textContent = '--';
            document.getElementById('program1-pressure-unit').textContent = lastValidPressureUnit;
            document.getElementById('program1-leak').textContent = '--';
            document.getElementById('program1-leak-unit').textContent = lastValidLeakUnit;
            document.getElementById('program1-result').textContent = '--';
            document.getElementById('program1-result').className = 'text-yellow-400 font-medium';
            document.getElementById('program2-pressure').textContent = '--';
            document.getElementById('program2-pressure-unit').textContent = lastValidPressureUnit;
            document.getElementById('program2-leak').textContent = '--';
            document.getElementById('program2-leak-unit').textContent = lastValidLeakUnit;
            document.getElementById('program2-result').textContent = '--';
            document.getElementById('program2-result').className = 'text-yellow-400 font-medium';
        }
        
        // 每秒更新一次数据
        setInterval(updateMonitorData, 1000);
        updateMonitorData();
        
        // 页面加载即启动状态轮询，不依赖UI启动按钮（支持PLC硬件触发）
        startStatusPolling();
        
        // 曲线卡片拖拽
        interact('#chart-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('chart-card');
                        localStorage.setItem('chart_card', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 250,
                            width: parseInt(card.style.width) || 700,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 500, height: 300 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                        // 调整图表大小
                        if (pressureChart) pressureChart.resize();
                        if (leakChart) leakChart.resize();
                    },
                    end(e) {
                        const card = document.getElementById('chart-card');
                        localStorage.setItem('chart_card', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 250,
                            width: parseInt(card.style.width) || 700,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            });
        
        // 加载曲线卡片位置
        const chartSaved = localStorage.getItem('chart_card');
        if (chartSaved) {
            try {
                const state = JSON.parse(chartSaved);
                const card = document.getElementById('chart-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 记录卡片拖拽
        interact('#record-card')
            .draggable({
                inertia: true,
                allowFrom: '.drag-handle',
                ignoreFrom: '.record-col-resizer, table, thead, tbody, th, td, .overflow-auto, button, input, select, textarea',
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('record-card');
                        localStorage.setItem('record_card', JSON.stringify({
                            left: parseInt(card.style.left) || 800,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 450,
                            height: parseInt(card.style.minHeight) || 500
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 350, height: 20 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('record-card');
                        localStorage.setItem('record_card', JSON.stringify({
                            left: parseInt(card.style.left) || 800,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 450,
                            height: parseInt(card.style.minHeight) || 500
                        }));
                    }
                }
            });
        
        // 加载记录卡片位置
        const recordSaved = localStorage.getItem('record_card');
        if (recordSaved) {
            try {
                const state = JSON.parse(recordSaved);
                const card = document.getElementById('record-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 状态指示灯卡片拖拽
        interact('#status-lights-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('status-lights-card');
                        localStorage.setItem('status_lights_card', JSON.stringify({
                            left: parseInt(card.style.left) || 720,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 320,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 180 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('status-lights-card');
                        localStorage.setItem('status_lights_card', JSON.stringify({
                            left: parseInt(card.style.left) || 720,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 320,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            });
        
        // 产品设置卡片拖拽
        interact('#product-settings-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#page-settings', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('product-settings-card');
                        localStorage.setItem('product_settings_card', JSON.stringify({
                            left: parseInt(card.style.left) || 20,
                            top: parseInt(card.style.top) || 20,
                            width: parseInt(card.style.width) || 400,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: true, right: true, bottom: true, top: true },
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('product-settings-card');
                        localStorage.setItem('product_settings_card', JSON.stringify({
                            left: parseInt(card.style.left) || 20,
                            top: parseInt(card.style.top) || 20,
                            width: parseInt(card.style.width) || 400,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            });
        
        interact('#settings-auth-bar')
            .draggable({
                ignoreFrom: 'input, button, select, option, textarea',
                listeners: {
                    start(event) {
                        const settingsPage = document.getElementById('page-settings');
                        const authBar = event.target;
                        if (!settingsPage || !settingsPage.classList.contains('settings-locked')) {
                            return;
                        }

                        if (!authBar.dataset.dragFree) {
                            const pageRect = settingsPage.getBoundingClientRect();
                            const barRect = authBar.getBoundingClientRect();
                            authBar.style.left = `${barRect.left - pageRect.left}px`;
                            authBar.style.top = `${barRect.top - pageRect.top}px`;
                            authBar.style.transform = 'none';
                            authBar.dataset.dragFree = '1';
                        }

                        authBar.classList.add('dragging');
                    },
                    move(event) {
                        const settingsPage = document.getElementById('page-settings');
                        const authBar = event.target;
                        if (!settingsPage || !settingsPage.classList.contains('settings-locked')) {
                            return;
                        }

                        const maxLeft = Math.max(0, settingsPage.clientWidth - authBar.offsetWidth);
                        const maxTop = Math.max(0, settingsPage.clientHeight - authBar.offsetHeight);
                        const nextLeft = Math.min(maxLeft, Math.max(0, (parseFloat(authBar.style.left) || 0) + event.dx));
                        const nextTop = Math.min(maxTop, Math.max(0, (parseFloat(authBar.style.top) || 0) + event.dy));
                        authBar.style.left = `${nextLeft}px`;
                        authBar.style.top = `${nextTop}px`;
                    },
                    end(event) {
                        event.target.classList.remove('dragging');
                    }
                }
            });

        interact('#operator-settings-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#page-settings', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) {
                        e.target.classList.remove('dragging');
                        const card = document.getElementById('operator-settings-card');
                        localStorage.setItem('operator_settings_card', JSON.stringify({
                            left: parseInt(card.style.left) || 20,
                            top: parseInt(card.style.top) || 20,
                            width: parseInt(card.style.width) || 280,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: true, right: true, bottom: true, top: true },
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('operator-settings-card');
                        localStorage.setItem('operator_settings_card', JSON.stringify({
                            left: parseInt(card.style.left) || 20,
                            top: parseInt(card.style.top) || 20,
                            width: parseInt(card.style.width) || 280,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            });

        // 加载操作人员卡片位置
        const operatorSettingsSaved = localStorage.getItem('operator_settings_card');
        if (operatorSettingsSaved) {
            try {
                const state = JSON.parse(operatorSettingsSaved);
                const card = document.getElementById('operator-settings-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }

        // 加载产品设置卡片位置
        const productSettingsSaved = localStorage.getItem('product_settings_card');
        if (productSettingsSaved) {
            try {
                const state = JSON.parse(productSettingsSaved);
                const card = document.getElementById('product-settings-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 加载状态指示灯卡片位置
        const statusLightsSaved = localStorage.getItem('status_lights_card');
        if (statusLightsSaved) {
            try {
                const state = JSON.parse(statusLightsSaved);
                const card = document.getElementById('status-lights-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 初始化图表
        let pressureChart, leakChart;
        let testParams = { fill_time: 3, stab_time: 2, test_time: 5 }; // 默认参数
        let chartProgramSlot = 0;
        let chartProgramNumber = 0;
        let chartParamsRequestSequence = 0;
        
        // 图表配置
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    type: 'linear',
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#9ca3af', font: { size: 10 } }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#9ca3af', font: { size: 10 } }
                }
            },
            elements: {
                point: { radius: 0, hitRadius: 5, hoverRadius: 3 },
                line: { tension: 0.3, borderWidth: 2 }
            }
        };
        
        function initCharts() {
            const pressureCtx = document.getElementById('pressure-chart').getContext('2d');
            const leakCtx = document.getElementById('leak-chart').getContext('2d');
            
            // 压力曲线 - X轴范围: 0 到 fill+stab+test
            const totalTime = testParams.fill_time + testParams.stab_time + testParams.test_time;
            pressureChart = new Chart(pressureCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: '压力',
                        data: [],
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartOptions,
                    scales: {
                        ...chartOptions.scales,
                        x: {
                            ...chartOptions.scales.x,
                            min: 0,
                            max: totalTime,
                            title: { display: true, text: '时间 (s)', color: '#6b7280', font: { size: 10 } }
                        },
                        y: {
                            ...chartOptions.scales.y,
                            title: { display: true, text: '压力', color: '#6b7280', font: { size: 10 } }
                        }
                    }
                }
            });
            
            // 泄漏量曲线 - X轴范围: 0 到 test_time
            leakChart = new Chart(leakCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: '泄漏量',
                        data: [],
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartOptions,
                    scales: {
                        ...chartOptions.scales,
                        x: {
                            ...chartOptions.scales.x,
                            min: 0,
                            max: testParams.test_time,
                            title: { display: true, text: 'Test Time (s)', color: '#6b7280', font: { size: 10 } }
                        },
                        y: {
                            ...chartOptions.scales.y,
                            title: { display: true, text: '泄漏量', color: '#6b7280', font: { size: 10 } }
                        }
                    }
                }
            });
        }
        
        // 获取测试参数
        async function loadTestParams(programNumber = null, requestSequence = null) {
            try {
                const selectedProgram = programNumber || (currentProductConfig ? parseInt(currentProductConfig.program1) : null);
                const paramsUrl = selectedProgram > 0 ? `/api/params?program=${selectedProgram}` : '/api/params';
                const response = await fetch(paramsUrl, { cache: 'no-store' });
                const data = await response.json();
                if (data.success) {
                    if (requestSequence !== null && requestSequence !== chartParamsRequestSequence) {
                        return false;
                    }
                    const fillTime = Number(data.fill_time) || 0;
                    const stabTime = Number(data.stab_time) || 0;
                    const testTime = Number(data.test_time) || 0;
                    testParams = {
                        ...data,
                        fill_time: fillTime,
                        stab_time: stabTime,
                        test_time: testTime
                    };
                    document.getElementById('fill-time').textContent = fillTime;
                    document.getElementById('stab-time').textContent = stabTime;
                    document.getElementById('test-time').textContent = testTime;
                    // 更新图表X轴范围
                    if (pressureChart) {
                        const totalTime = Number((fillTime + stabTime + testTime).toFixed(2));
                        pressureChart.options.scales.x.max = totalTime;
                        pressureChart.update('none');
                    }
                    if (leakChart) {
                        leakChart.options.scales.x.max = Number(testTime.toFixed(2));
                        leakChart.update('none');
                    }
                    return true;
                }
            } catch(e) {
                console.error('读取程序时间参数失败:', e);
            }
            return false;
        }
        
        // 更新曲线数据
        let pressureData = [];
        let leakData = [];
        let testStartTime = null;
        let testPhaseStartTime = null;
        let lastStepCode = 0;
        let postTestIdleCaptureStartedAt = null;

        function configuredProgramForSlot(slot) {
            if (!currentProductConfig) return 0;
            const value = Number(slot) === 2
                ? currentProductConfig.program2
                : currentProductConfig.program1;
            return parseInt(value) || 0;
        }

        function resetChartCaptureForProgramSwitch() {
            pressureData = [];
            leakData = [];
            testStartTime = null;
            testPhaseStartTime = null;
            postTestIdleCaptureStartedAt = null;
            lastStepCode = 0;
            if (pressureChart) {
                pressureChart.data.datasets[0].data = [];
                pressureChart.update('none');
            }
            if (leakChart) {
                leakChart.data.datasets[0].data = [];
                leakChart.update('none');
            }
        }

        async function switchChartToProgramSlot(slot) {
            const normalizedSlot = Number(slot);
            if (normalizedSlot !== 1 && normalizedSlot !== 2) return;
            const programNumber = configuredProgramForSlot(normalizedSlot);
            if (programNumber <= 0) return;
            if (chartProgramSlot === normalizedSlot && chartProgramNumber === programNumber) return;

            chartProgramSlot = normalizedSlot;
            chartProgramNumber = programNumber;
            const requestSequence = ++chartParamsRequestSequence;
            resetChartCaptureForProgramSwitch();
            const loaded = await loadTestParams(programNumber, requestSequence);
            if (!loaded && requestSequence === chartParamsRequestSequence) {
                chartProgramSlot = 0;
                chartProgramNumber = 0;
            }
        }
        
        async function updateCharts() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();
                
                if (data.success) {
                    const now = Date.now() / 1000;
                    let stepCode = data.step_code != null ? data.step_code : 65535;
                    
                    // 调试: 显示实际 step_code
                    console.log('API数据:', {step_code: stepCode, pressure: data.pressure, leak: data.leak});
                    
                    // 正确的step code定义
                    const stepNames = {
                        0: 'Pre-fill', 1: 'Pre-dump', 2: 'Sealed fill', 3: 'Sealed stab',
                        4: 'Fill', 5: 'Stab', 6: 'Test', 7: 'Dump', 65535: '空闲'
                    };
                    const statusEl = document.getElementById('chart-status');
                    statusEl.textContent = `${stepNames[stepCode] || stepCode} (${stepCode})`;
                    
                    // Fill开始 (step_code = 4)
                    const isFillStart = stepCode === 4 && lastStepCode !== 4;
                    const isTestStart = stepCode === 6 && lastStepCode !== 6;
                    // Test结束 (从Test=6变为其他)
                    const isTestEnd = lastStepCode === 6 && stepCode !== 6;
                    const isPostTestIdleCaptureStart = isTestEnd && stepCode === 65535;
                    const postTestIdleCaptureActive =
                        isPostTestIdleCaptureStart ||
                        (
                            stepCode === 65535 &&
                            postTestIdleCaptureStartedAt !== null &&
                            (now - postTestIdleCaptureStartedAt) <= (FINAL_IDLE_CAPTURE_MS / 1000)
                        );
                    
                    // Fill开始时清空并开始记录
                    if (isFillStart) {
                        testStartTime = now;
                        testPhaseStartTime = null;
                        postTestIdleCaptureStartedAt = null;
                        pressureData = [];
                        leakData = [];
                        // 清空泄漏量曲线
                        leakChart.data.datasets[0].data = [];
                        leakChart.update('none');
                        console.log('Fill开始(stepcode=4), 清空压力和泄漏量数据');
                    }
                    
                    // 正在测试中 (Fill=4, Stab=5, Test=6)
                    if (((stepCode >= 4 && stepCode <= 6) || postTestIdleCaptureActive) && testStartTime) {
                        const elapsed = now - testStartTime;
                        const totalTime = Number((testParams.fill_time + testParams.stab_time + testParams.test_time).toFixed(2));
                        
                        // 压力数据 - 整个fill+stab+test阶段
                        if (elapsed <= totalTime) {
                            pressureData.push({ x: parseFloat(elapsed.toFixed(2)), y: data.pressure });
                            pressureChart.data.datasets[0].data = pressureData;
                            pressureChart.update('none');
                        }
                        
                        // 泄漏量数据 - 只在Test阶段(step_code=6)
                        if (stepCode === 6 || postTestIdleCaptureActive) {
                            if (isTestStart || testPhaseStartTime === null) {
                                testPhaseStartTime = now;
                                leakData = [];
                                leakChart.data.datasets[0].data = [];
                            }
                            const testElapsed = now - testPhaseStartTime;
                            if (testElapsed >= 0 && testElapsed <= (testParams.test_time + FINAL_IDLE_CAPTURE_MS / 1000)) {
                                leakData.push({ x: parseFloat(testElapsed.toFixed(2)), y: data.leak });
                                leakChart.data.datasets[0].data = leakData;
                                leakChart.update('none');
                            }
                        }
                        
                        statusEl.className = 'text-xs px-2 py-0.5 rounded bg-green-600 text-white';
                    } else {
                        statusEl.className = 'text-xs px-2 py-0.5 rounded bg-gray-600 text-gray-300';
                    }
                    
                    // Test结束 - 保持曲线，不清空
                    if (isTestEnd) {
                        console.log('Test结束, 保持曲线');
                        if (stepCode === 65535) {
                            postTestIdleCaptureStartedAt = now;
                        } else {
                            testStartTime = null;
                            testPhaseStartTime = null;
                            postTestIdleCaptureStartedAt = null;
                        }
                    }

                    if (
                        postTestIdleCaptureStartedAt !== null &&
                        stepCode === 65535 &&
                        !postTestIdleCaptureActive
                    ) {
                        testStartTime = null;
                        testPhaseStartTime = null;
                        postTestIdleCaptureStartedAt = null;
                    }
                    
                    lastStepCode = stepCode;
                }
            } catch(e) {
                console.error('更新曲线错误:', e);
            }
        }
        
        // 初始化
        initCharts();
        loadTestParams();
        setInterval(updateCharts, 200); // 每200ms更新一次曲线
        // ATEQ parameters are loaded on page entry and program changes.
        
        loadState();
        
        // ========== 测试记录卡片功能 ==========
        let lastTestData = null;
        let currentSerialNumber = null;
        let recordsFilterKey = null;
        let renderedRecordKeys = new Set();
        const RECORD_COLUMN_WIDTHS_KEY = 'test-record-column-widths-v1';
        const DEFAULT_RECORD_COLUMN_WIDTHS = [52, 165, 120, 170, 110, 110, 110, 110, 90, 120, 96];
        let recordColumnWidths = [...DEFAULT_RECORD_COLUMN_WIDTHS];
        let activeRecordColumnResize = null;
        let recordsColumnResizeInitialized = false;

        function clampRecordColumnWidth(index, width) {
            const nextWidth = Number(width);
            if (!Number.isFinite(nextWidth)) {
                return DEFAULT_RECORD_COLUMN_WIDTHS[index] || 1;
            }
            return Math.max(1, Math.round(nextWidth));
        }

        function loadRecordColumnWidths() {
            try {
                const saved = localStorage.getItem(RECORD_COLUMN_WIDTHS_KEY);
                if (!saved) return;

                const parsed = JSON.parse(saved);
                if (!Array.isArray(parsed) || parsed.length !== DEFAULT_RECORD_COLUMN_WIDTHS.length) {
                    return;
                }

                recordColumnWidths = DEFAULT_RECORD_COLUMN_WIDTHS.map((defaultWidth, index) => {
                    const parsedWidth = Number(parsed[index]);
                    return Number.isFinite(parsedWidth)
                        ? clampRecordColumnWidth(index, parsedWidth)
                        : defaultWidth;
                });
            } catch (error) {
                console.warn('加载测试记录列宽失败:', error);
            }
        }

        function persistRecordColumnWidths() {
            try {
                localStorage.setItem(RECORD_COLUMN_WIDTHS_KEY, JSON.stringify(recordColumnWidths));
            } catch (error) {
                console.warn('保存测试记录列宽失败:', error);
            }
        }

        function applyRecordColumnWidths() {
            const table = document.getElementById('records-display-table');
            const cols = document.querySelectorAll('#records-table-colgroup col');
            if (!table || !cols.length) return;

            let totalWidth = 0;
            cols.forEach((col, index) => {
                const width = clampRecordColumnWidth(index, recordColumnWidths[index] ?? DEFAULT_RECORD_COLUMN_WIDTHS[index]);
                recordColumnWidths[index] = width;
                col.style.width = `${width}px`;
                totalWidth += width;
            });

            const containerWidth = table.parentElement?.clientWidth || 0;
            const appliedWidth = Math.max(totalWidth, containerWidth);
            table.style.width = `${appliedWidth}px`;
        }

        function handleRecordColumnResizeMove(event) {
            if (!activeRecordColumnResize) return;

            const nextWidth = clampRecordColumnWidth(
                activeRecordColumnResize.index,
                activeRecordColumnResize.startWidth + (event.clientX - activeRecordColumnResize.startX)
            );

            if (nextWidth === recordColumnWidths[activeRecordColumnResize.index]) {
                return;
            }

            recordColumnWidths[activeRecordColumnResize.index] = nextWidth;
            applyRecordColumnWidths();
        }

        function stopRecordColumnResize() {
            if (!activeRecordColumnResize) return;

            activeRecordColumnResize = null;
            document.body.classList.remove('records-col-resizing');
            document.removeEventListener('pointermove', handleRecordColumnResizeMove);
            document.removeEventListener('pointerup', stopRecordColumnResize);
            document.removeEventListener('pointercancel', stopRecordColumnResize);
            persistRecordColumnWidths();
        }

        function startRecordColumnResize(event) {
            const handle = event.target.closest('.record-col-resizer');
            if (!handle) return;

            event.preventDefault();
            event.stopPropagation();

            const index = Number(handle.dataset.colIndex);
            if (!Number.isFinite(index)) return;

            activeRecordColumnResize = {
                index,
                startX: event.clientX,
                startWidth: recordColumnWidths[index] ?? DEFAULT_RECORD_COLUMN_WIDTHS[index]
            };

            document.body.classList.add('records-col-resizing');
            document.addEventListener('pointermove', handleRecordColumnResizeMove);
            document.addEventListener('pointerup', stopRecordColumnResize);
            document.addEventListener('pointercancel', stopRecordColumnResize);
        }

        function initResizableRecordsTable() {
            loadRecordColumnWidths();
            applyRecordColumnWidths();

            if (recordsColumnResizeInitialized) {
                return;
            }

            document.querySelectorAll('#records-display-table .record-col-resizer').forEach(handle => {
                handle.addEventListener('pointerdown', startRecordColumnResize);
            });

            recordsColumnResizeInitialized = true;
        }
        
        function getRecordFilterState() {
            const selector = document.getElementById('product-selector');
            const selectedProduct = selector?.value || '';
            const selectedProductText = selector?.selectedOptions[0]?.text || '';
            const filterKey = selectedProduct ? selectedProductText : '__ALL__';
            return { selectedProduct, selectedProductText, filterKey };
        }

        function buildRecordKey(record) {
            if (record && record.id !== null && record.id !== undefined) {
                return `id:${record.id}`;
            }
            return [
                record?.test_time || '',
                record?.daily_serial || '',
                record?.serial_number || '',
                record?.qr_code || ''
            ].join('|');
        }

        function formatRecordTimestamp(testTime) {
            const date = new Date(testTime);
            return date.getFullYear() + '-' +
                String(date.getMonth() + 1).padStart(2, '0') + '-' +
                String(date.getDate()).padStart(2, '0') + ' ' +
                String(date.getHours()).padStart(2, '0') + ':' +
                String(date.getMinutes()).padStart(2, '0') + ':' +
                String(date.getSeconds()).padStart(2, '0');
        }

        function formatRecordValue(value, unit) {
            if (value === null || value === undefined) {
                return '-';
            }
            return `${value.toFixed(3)} ${unit}`;
        }

        function renderRecordRow(record) {
            const recordKey = buildRecordKey(record);
            const legacyPressureUnit = record.pressure_unit || lastValidPressureUnit || '--';
            const legacyLeakUnit = record.leak_unit || lastValidLeakUnit || '--';
            const pressure1Text = formatRecordValue(record.pressure1, record.pressure1_unit || legacyPressureUnit);
            const leak1Text = formatRecordValue(record.leak1, record.leak1_unit || legacyLeakUnit);
            const pressure2Text = formatRecordValue(record.pressure2, record.pressure2_unit || legacyPressureUnit);
            const leak2Text = formatRecordValue(record.leak2, record.leak2_unit || legacyLeakUnit);
            const formattedTime = formatRecordTimestamp(record.test_time);

            return `
                            <tr class="hover:bg-gray-600/30" data-record-key="${recordKey}">
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${record.daily_serial}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formattedTime}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${record.serial_number || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.qr_code || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${pressure1Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${leak1Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${pressure2Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${leak2Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center">
                                    <span class="px-1 py-0.5 rounded font-medium text-xs ${record.test_result === 'PASS' ? 'bg-green-600 text-white' : 'bg-red-600 text-white'}">
                                        ${record.test_result}
                                    </span>
                                </td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.product_model || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.operator || '-'}</td>
                            </tr>
                        `;
        }

        function updateRecordStats(records) {
            const total = records.length;
            const passCount = records.filter(r => r.test_result === 'PASS').length;
            const passRate = total > 0 ? Math.round((passCount / total) * 100) : 0;

            document.getElementById('today-count').textContent = `今日: ${total}`;
            document.getElementById('pass-rate').textContent = `合格率: ${passRate}%`;
        }

        function renderRecordsTable(records) {
            const tbody = document.getElementById('records-table');
            if (!records || records.length === 0) {
                tbody.innerHTML = '<tr><td colspan="11" class="py-8 text-center text-gray-500">暂无今日记录</td></tr>';
                renderedRecordKeys = new Set();
                return;
            }

            tbody.innerHTML = records.map(renderRecordRow).join('');
            renderedRecordKeys = new Set(records.map(buildRecordKey));
        }

        function prependNewRecordRows(records) {
            if (!records || records.length === 0) {
                return;
            }

            const tbody = document.getElementById('records-table');
            const rowsHtml = records.map(renderRecordRow).join('');
            const hasPlaceholder = tbody.querySelector('td[colspan="11"]');

            if (hasPlaceholder) {
                tbody.innerHTML = rowsHtml;
            } else {
                tbody.insertAdjacentHTML('afterbegin', rowsHtml);
            }

            records.forEach(record => renderedRecordKeys.add(buildRecordKey(record)));
        }
        
        // 监听测试结束，保存测试数据
        let testEndDetected = false;
        const originalUpdateCharts = updateCharts;
        updateCharts = async function() {
            await originalUpdateCharts();
            
            // 检测测试结束 (从Test=6变为其他)
            if (lastStepCode === 6 && stepCode !== 6) {
                // 测试刚结束，获取最终数据
                try {
                    const response = await fetch('/api/data');
                    const data = await response.json();
                    if (data.success) {
                        lastTestData = {
                            pressure1: data.pressure,
                            leak1: data.leak,
                            pressure2: null,
                            leak2: null,
                            result: 'PASS' // 根据泄漏量判断
                        };
                    }
                } catch(e) {}
            }
        };
        
        // 加载当日记录列表
        async function loadRecords(options = {}) {
            try {
                const { forceReload = false } = options;
                const response = await fetch('/api/records/today', { cache: 'no-store' });
                const data = await response.json();
                
                if (data.success) {
                    // 获取当前选中的产品型号
                    const { selectedProduct, selectedProductText, filterKey } = getRecordFilterState();
                    let visibleRecords = data.records;
                    if (selectedProduct && selectedProduct !== '') {
                        visibleRecords = data.records.filter(r => r.product_model === selectedProductText);
                    }

                    const sortedVisibleRecords = visibleRecords
                        .slice()
                        .sort((a, b) => new Date(b.test_time) - new Date(a.test_time));

                    updateRecordStats(sortedVisibleRecords);

                    const currentKeys = new Set(sortedVisibleRecords.map(buildRecordKey));
                    const filterChanged = recordsFilterKey !== filterKey;
                    const hasRemovedRecords = Array.from(renderedRecordKeys).some(key => !currentKeys.has(key));

                    if (forceReload || filterChanged || renderedRecordKeys.size === 0 || hasRemovedRecords) {
                        renderRecordsTable(sortedVisibleRecords);
                        recordsFilterKey = filterKey;
                        return;
                    }

                    const incomingRecords = sortedVisibleRecords.filter(record => !renderedRecordKeys.has(buildRecordKey(record)));
                    if (incomingRecords.length > 0) {
                        prependNewRecordRows(incomingRecords);
                    }

                    recordsFilterKey = filterKey;
                    renderedRecordKeys = currentKeys;
                    return;
                    
                    // 过滤记录：只显示已选产品型号的记录
                    let filteredRecords = data.records;
                    if (selectedProduct && selectedProduct !== '') {
                        filteredRecords = data.records.filter(r => r.product_model === selectedProductText);
                    }
                    
                    // 计算过滤后的统计
                    const total = filteredRecords.length;
                    const passCount = filteredRecords.filter(r => r.test_result === 'PASS').length;
                    const passRate = total > 0 ? Math.round((passCount / total) * 100) : 0;
                    
                    // 更新统计
                    document.getElementById('today-count').textContent = `今日: ${total}`;
                    document.getElementById('pass-rate').textContent = `合格率: ${passRate}%`;
                    
                    // 更新表格
                    const tbody = document.getElementById('records-table');
                    if (filteredRecords.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="11" class="py-8 text-center text-gray-500">暂无今日记录</td></tr>';
                    } else {
                        // 按时间降序排序
                        const sortedRecords = filteredRecords.sort((a, b) => new Date(b.test_time) - new Date(a.test_time));
                        
                        tbody.innerHTML = sortedRecords.map((r, index) => {
                            // 格式化时间: YYYY-MM-DD 00:00:00
                            const date = new Date(r.test_time);
                            const formattedTime = date.getFullYear() + '-' + 
                                String(date.getMonth() + 1).padStart(2, '0') + '-' + 
                                String(date.getDate()).padStart(2, '0') + ' ' +
                                String(date.getHours()).padStart(2, '0') + ':' + 
                                String(date.getMinutes()).padStart(2, '0') + ':' + 
                                String(date.getSeconds()).padStart(2, '0');
                            
                            // 格式化：直接复制程序号1结果的显示内容
                            const legacyPressureUnit = r.pressure_unit || lastValidPressureUnit || '--';
                            const legacyLeakUnit = r.leak_unit || lastValidLeakUnit || '--';
                            const formatLeak = (val, unit) => val === null || val === undefined ? '-' : val.toFixed(3) + ' ' + unit;
                            const formatPressure = (val, unit) => val === null || val === undefined ? '-' : val.toFixed(3) + ' ' + unit;
                            
                            return `
                            <tr class="hover:bg-gray-600/30">
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${r.daily_serial}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formattedTime}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${r.serial_number || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${r.qr_code || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formatPressure(r.pressure1, r.pressure1_unit || legacyPressureUnit)}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formatLeak(r.leak1, r.leak1_unit || legacyLeakUnit)}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formatPressure(r.pressure2, r.pressure2_unit || legacyPressureUnit)}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formatLeak(r.leak2, r.leak2_unit || legacyLeakUnit)}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center">
                                    <span class="px-1 py-0.5 rounded font-medium text-xs ${r.test_result === 'PASS' ? 'bg-green-600 text-white' : 'bg-red-600 text-white'}">
                                        ${r.test_result}
                                    </span>
                                </td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${r.product_model || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${r.operator || '-'}</td>
                            </tr>
                        `}).join('');
                    }
                }
            } catch(e) {
                console.error('加载记录失败:', e);
            }
        }
        
        // 刷新记录
        document.getElementById('btn-refresh').addEventListener('click', () => loadRecords({ forceReload: true }));
        
        // 初始加载
        initResizableRecordsTable();
        loadRecords({ forceReload: true });
        setInterval(loadRecords, 10000); // 每10秒刷新
        // 标签页切换
        document.getElementById('tab-test').addEventListener('click', function() {
            switchTab('test');
        });
        
        document.getElementById('tab-manual').addEventListener('click', function() {
            switchTab('manual');
        });
        
        document.getElementById('tab-settings').addEventListener('click', function() {
            switchTab('settings');
        });
        
        document.getElementById('tab-query').addEventListener('click', function() {
            switchTab('query');
        });
        
        // 自动/手动模式切换
        function toggleAutoManual(checkbox) {
            const isManual = checkbox.checked;
            const modeText = document.getElementById('mode-text');
            const statusBadge = document.getElementById('manual-status-badge');
            const manualButtons = document.querySelectorAll('.plc-manual-btn');
            
            if (isManual) {
                modeText.textContent = '手动模式';
                modeText.classList.remove('text-gray-300');
                modeText.classList.add('text-blue-400');
                statusBadge.textContent = '手动模式-可操作';
                statusBadge.classList.remove('bg-red-600');
                statusBadge.classList.add('bg-green-600');
                
                // 启用手动控制按钮
                manualButtons.forEach(btn => {
                    btn.disabled = false;
                });
                
                // 发送PLC指令设置M26.0为手动模式
                sendPLCCommand('M26.0', true);
            } else {
                modeText.textContent = '自动模式';
                modeText.classList.add('text-gray-300');
                modeText.classList.remove('text-blue-400');
                statusBadge.textContent = '自动模式-不可操作';
                statusBadge.classList.add('bg-red-600');
                statusBadge.classList.remove('bg-green-600');
                
                // 禁用手动控制按钮
                manualButtons.forEach(btn => {
                    btn.disabled = true;
                });
                
                // 发送PLC指令设置M26.0为自动模式
                sendPLCCommand('M26.0', false);
            }
        }
        
        // PLC手动操作
        function operatePLC(address, button) {
            // 添加点击效果
            button.classList.add('ring-2', 'ring-blue-500');
            setTimeout(() => {
                button.classList.remove('ring-2', 'ring-blue-500');
            }, 200);
            
            // 发送PLC指令
            sendPLCCommand(address, true);
            
            // 显示操作提示
            showNotification(`已发送指令: ${address}`, 'info');
        }
        
        // 发送PLC指令
        async function sendPLCCommand(address, value) {
            return { success: false, message: 'PLC关联已取消' };
        }
        
        // 显示通知
        function showNotification(message, type = 'info') {
            const notification = document.createElement('div');
            const colors = {
                info: 'bg-blue-600',
                success: 'bg-green-600',
                error: 'bg-red-600',
                warning: 'bg-yellow-600'
            };
            
            notification.className = `fixed top-14 right-4 ${colors[type]} text-white px-4 py-2 rounded-lg shadow-lg z-50 transition-opacity duration-300`;
            notification.textContent = message;
            document.body.appendChild(notification);
            
            setTimeout(() => {
                notification.style.opacity = '0';
                setTimeout(() => notification.remove(), 300);
            }, 3000);
        }
        
        function switchTab(tabName) {
            // 隐藏所有标签页
            document.querySelectorAll('.tab-pane').forEach(pane => {
                pane.classList.remove('active');
            });
            
            // 移除所有标签按钮的活动状态
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.remove('active');
                btn.classList.add('text-gray-400');
                btn.classList.remove('text-white');
                btn.classList.remove('border-blue-500');
                btn.classList.add('border-transparent');
            });
            
            // 显示选中的标签页
            document.getElementById(`page-${tabName}`).classList.add('active');
            if (tabName === 'query') {
                applyQueryColumnWidths();
            }
            
            // 激活选中的标签按钮
            const activeBtn = document.getElementById(`tab-${tabName}`);
            activeBtn.classList.add('active');
            activeBtn.classList.remove('text-gray-400');
            activeBtn.classList.add('text-white');
            activeBtn.classList.add('border-blue-500');
            activeBtn.classList.remove('border-transparent');
            
            // 切换到设置页面时加载设置
            if (tabName === 'settings') {
                loadSettings();
            }
            
            // 切换到测试页面时刷新产品选择器
            if (tabName === 'test') {
                loadProductSelector();
            }
            
            // 切换到全量查询页面时加载产品型号
            if (tabName === 'query') {
                loadQueryProductModels();
                // 默认查询当日数据
                const today = new Date().toISOString().split('T')[0];
                document.getElementById('query-start-date').value = today;
                document.getElementById('query-end-date').value = today;
            }

            window.requestAnimationFrame(() => normalizeVisibleCardLayouts(tabName));
        }
        
        // ==================== 全量查询功能 ====================
        let queryCurrentPage = 1;
        let queryTotalPages = 0;
        let queryTotalRecords = 0;
        const QUERY_COLUMN_WIDTHS_KEY = 'query-record-column-widths-v1';
        const DEFAULT_QUERY_COLUMN_WIDTHS = [56, 168, 180, 120, 120, 110, 110, 110, 110, 90, 96];
        let queryColumnWidths = [...DEFAULT_QUERY_COLUMN_WIDTHS];
        let activeQueryColumnResize = null;
        let queryColumnResizeInitialized = false;

        function clampQueryColumnWidth(index, width) {
            const nextWidth = Number(width);
            if (!Number.isFinite(nextWidth)) {
                return DEFAULT_QUERY_COLUMN_WIDTHS[index] || 1;
            }
            return Math.max(1, Math.round(nextWidth));
        }

        function loadQueryColumnWidths() {
            try {
                const saved = localStorage.getItem(QUERY_COLUMN_WIDTHS_KEY);
                if (!saved) return;

                const parsed = JSON.parse(saved);
                if (!Array.isArray(parsed) || parsed.length !== DEFAULT_QUERY_COLUMN_WIDTHS.length) {
                    return;
                }

                queryColumnWidths = DEFAULT_QUERY_COLUMN_WIDTHS.map((defaultWidth, index) => {
                    const parsedWidth = Number(parsed[index]);
                    return Number.isFinite(parsedWidth)
                        ? clampQueryColumnWidth(index, parsedWidth)
                        : defaultWidth;
                });
            } catch (error) {
                console.warn('加载查询列宽失败:', error);
            }
        }

        function persistQueryColumnWidths() {
            try {
                localStorage.setItem(QUERY_COLUMN_WIDTHS_KEY, JSON.stringify(queryColumnWidths));
            } catch (error) {
                console.warn('保存查询列宽失败:', error);
            }
        }

        function applyQueryColumnWidths() {
            const table = document.getElementById('query-results-table');
            const cols = document.querySelectorAll('#query-table-colgroup col');
            if (!table || !cols.length) return;

            let totalWidth = 0;
            cols.forEach((col, index) => {
                const width = clampQueryColumnWidth(index, queryColumnWidths[index] ?? DEFAULT_QUERY_COLUMN_WIDTHS[index]);
                queryColumnWidths[index] = width;
                col.style.width = `${width}px`;
                totalWidth += width;
            });

            const containerWidth = table.parentElement?.clientWidth || 0;
            const appliedWidth = Math.max(totalWidth, containerWidth);
            table.style.width = `${appliedWidth}px`;
        }

        function handleQueryColumnResizeMove(event) {
            if (!activeQueryColumnResize) return;

            const nextWidth = clampQueryColumnWidth(
                activeQueryColumnResize.index,
                activeQueryColumnResize.startWidth + (event.clientX - activeQueryColumnResize.startX)
            );

            if (nextWidth === queryColumnWidths[activeQueryColumnResize.index]) {
                return;
            }

            queryColumnWidths[activeQueryColumnResize.index] = nextWidth;
            applyQueryColumnWidths();
        }

        function stopQueryColumnResize() {
            if (!activeQueryColumnResize) return;

            activeQueryColumnResize = null;
            document.body.classList.remove('records-col-resizing');
            document.removeEventListener('pointermove', handleQueryColumnResizeMove);
            document.removeEventListener('pointerup', stopQueryColumnResize);
            document.removeEventListener('pointercancel', stopQueryColumnResize);
            persistQueryColumnWidths();
        }

        function startQueryColumnResize(event) {
            const handle = event.target.closest('.record-col-resizer');
            if (!handle) return;

            event.preventDefault();
            event.stopPropagation();

            const index = Number(handle.dataset.queryColIndex);
            if (!Number.isFinite(index)) return;

            activeQueryColumnResize = {
                index,
                startX: event.clientX,
                startWidth: queryColumnWidths[index] ?? DEFAULT_QUERY_COLUMN_WIDTHS[index]
            };

            document.body.classList.add('records-col-resizing');
            document.addEventListener('pointermove', handleQueryColumnResizeMove);
            document.addEventListener('pointerup', stopQueryColumnResize);
            document.addEventListener('pointercancel', stopQueryColumnResize);
        }

        function initResizableQueryTable() {
            loadQueryColumnWidths();
            applyQueryColumnWidths();

            if (queryColumnResizeInitialized) {
                return;
            }

            document.querySelectorAll('#query-results-table .record-col-resizer').forEach(handle => {
                handle.addEventListener('pointerdown', startQueryColumnResize);
            });

            queryColumnResizeInitialized = true;
        }
         
        // 加载产品型号到查询页面
        async function loadQueryProductModels() {
            try {
                const response = await fetch('/api/products');
                const data = await response.json();
                
                const select = document.getElementById('query-product-model');
                select.innerHTML = '<option value="">全部</option>';
                
                if (data.success && data.products) {
                    data.products.forEach(product => {
                        const option = document.createElement('option');
                        option.value = product.model;
                        option.textContent = product.model;
                        select.appendChild(option);
                    });
                }
            } catch(e) {
                console.error('加载产品型号失败:', e);
            }
        }
        
        // 构建查询参数
        function buildQueryParams(page = 1) {
            const params = new URLSearchParams();
            params.append('page', page);
            params.append('limit', document.getElementById('query-limit').value);
            
            const startDate = document.getElementById('query-start-date').value;
            const endDate = document.getElementById('query-end-date').value;
            const productModel = document.getElementById('query-product-model').value;
            const result = document.getElementById('query-result').value;
            const qrCode = document.getElementById('query-qr-code').value;
            const serial = document.getElementById('query-serial').value;
            
            if (startDate) params.append('start_date', startDate);
            if (endDate) params.append('end_date', endDate);
            if (productModel) params.append('product_model', productModel);
            if (result) params.append('result', result);
            if (qrCode) params.append('qr_code', qrCode);
            if (serial) params.append('serial', serial);
            
            return params;
        }
        
        // 执行查询
        async function executeQuery(page = 1) {
            try {
                const params = buildQueryParams(page);
                const response = await fetch(`/api/query/records?${params.toString()}`);
                const data = await response.json();
                
                if (data.success) {
                    queryCurrentPage = data.page;
                    queryTotalPages = data.total_pages;
                    queryTotalRecords = data.total;
                    
                    // 更新统计信息
                    document.getElementById('stat-total').textContent = data.total;
                    document.getElementById('stat-pass').textContent = data.pass_count;
                    document.getElementById('stat-fail').textContent = data.fail_count;
                    document.getElementById('stat-rate').textContent = data.pass_rate + '%';
                    
                    // 更新分页信息
                    document.getElementById('query-page-info').textContent = `第 ${data.page} 页 / 共 ${data.total_pages} 页`;
                    document.getElementById('page-input').value = data.page;
                    
                    // 更新表格
                    updateQueryTable(data.records);
                    
                    // 更新分页按钮状态
                    updatePaginationButtons();
                    
                    showNotification(`查询完成，共 ${data.total} 条记录`, 'success');
                } else {
                    showNotification('查询失败: ' + data.message, 'error');
                }
            } catch(e) {
                console.error('查询失败:', e);
                showNotification('查询失败: ' + e.message, 'error');
            }
        }
        
        // 更新查询表格
        function updateQueryTable(records) {
            const tbody = document.getElementById('query-table-body');

            if (!records || records.length === 0) {
                tbody.innerHTML = '<tr><td colspan="11" class="text-center py-8 text-gray-500">暂无数据</td></tr>';
                return;
            }

            const pressureUnit = document.getElementById('program1-pressure-unit')?.textContent || lastValidPressureUnit;
            const leakUnit = document.getElementById('program1-leak-unit')?.textContent || lastValidLeakUnit;

            tbody.innerHTML = records.map((r, index) => `
                <tr class="hover:bg-gray-700/50">
                    <td class="py-1 px-2 border border-gray-600 text-center">${(queryCurrentPage - 1) * parseInt(document.getElementById('query-limit').value) + index + 1}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.test_time || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.qr_code || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.serial_number || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.product_model || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.pressure1 !== null ? r.pressure1.toFixed(3) + ' ' + pressureUnit : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.leak1 !== null ? r.leak1.toFixed(3) + ' ' + leakUnit : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.pressure2 !== null ? r.pressure2.toFixed(3) + ' ' + pressureUnit : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.leak2 !== null ? r.leak2.toFixed(3) + ' ' + leakUnit : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center ${r.test_result === 'PASS' ? 'text-green-400' : 'text-red-400'}">${r.test_result || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.operator || '-'}</td>
                </tr>
            `).join('');
        }
        
        // 更新分页按钮状态
        function updatePaginationButtons() {
            const btnFirst = document.getElementById('btn-page-first');
            const btnPrev = document.getElementById('btn-page-prev');
            const btnNext = document.getElementById('btn-page-next');
            const btnLast = document.getElementById('btn-page-last');
            
            btnFirst.disabled = queryCurrentPage <= 1;
            btnPrev.disabled = queryCurrentPage <= 1;
            btnNext.disabled = queryCurrentPage >= queryTotalPages;
            btnLast.disabled = queryCurrentPage >= queryTotalPages;
        }
        
        // 重置查询条件
        function resetQuery() {
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('query-start-date').value = today;
            document.getElementById('query-end-date').value = today;
            document.getElementById('query-product-model').value = '';
            document.getElementById('query-result').value = '';
            document.getElementById('query-qr-code').value = '';
            document.getElementById('query-serial').value = '';
            document.getElementById('query-limit').value = '20';
            
            // 清空统计和表格
            document.getElementById('stat-total').textContent = '0';
            document.getElementById('stat-pass').textContent = '0';
            document.getElementById('stat-fail').textContent = '0';
            document.getElementById('stat-rate').textContent = '0%';
            document.getElementById('query-table-body').innerHTML = '<tr><td colspan="11" class="text-center py-8 text-gray-500">请执行查询</td></tr>';
            document.getElementById('query-page-info').textContent = '第 1 页 / 共 0 页';
        }
        
        // 导出CSV
        async function exportCSV() {
            try {
                const params = buildQueryParams(1);
                params.set('limit', '10000'); // 导出最多10000条
                params.set('export', '1');
                params.set('pressure_unit', lastValidPressureUnit);
                params.set('leak_unit', lastValidLeakUnit);
                
                const response = await fetch(`/api/query/export?${params.toString()}`);
                const blob = await response.blob();
                
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `test_records_${new Date().toISOString().split('T')[0]}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                showNotification('导出成功', 'success');
            } catch(e) {
                console.error('导出失败:', e);
                showNotification('导出失败: ' + e.message, 'error');
            }
        }
        
        // 绑定查询页面事件
        document.addEventListener('DOMContentLoaded', function() {
            initResizableQueryTable();
            // 查询按钮
            document.getElementById('btn-query-search').addEventListener('click', function() {
                executeQuery(1);
            });
            
            // 重置按钮
            document.getElementById('btn-query-reset').addEventListener('click', resetQuery);
            
            // 导出按钮
            document.getElementById('btn-query-export').addEventListener('click', exportCSV);
            
            // 分页按钮
            document.getElementById('btn-page-first').addEventListener('click', function() {
                executeQuery(1);
            });
            
            document.getElementById('btn-page-prev').addEventListener('click', function() {
                if (queryCurrentPage > 1) {
                    executeQuery(queryCurrentPage - 1);
                }
            });
            
            document.getElementById('btn-page-next').addEventListener('click', function() {
                if (queryCurrentPage < queryTotalPages) {
                    executeQuery(queryCurrentPage + 1);
                }
            });
            
            document.getElementById('btn-page-last').addEventListener('click', function() {
                executeQuery(queryTotalPages);
            });
            
            document.getElementById('btn-page-go').addEventListener('click', function() {
                const page = parseInt(document.getElementById('page-input').value);
                if (page >= 1 && page <= queryTotalPages) {
                    executeQuery(page);
                } else {
                    showNotification('请输入有效的页码', 'warning');
                }
            });
            
            // 回车跳转
            document.getElementById('page-input').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    document.getElementById('btn-page-go').click();
                }
            });

            window.requestAnimationFrame(() => normalizeVisibleCardLayouts('test'));
        });
        
        // 产品列表数据
        let products = [];
        let productCounter = 0;

        function escapeAttr(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/"/g, '&quot;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }

        function renderOperatorList() {
            const container = document.getElementById('operator-list');
            if (!container) return;
            const accounts = readConfiguredAccounts();
            let html = '';
            for (let i = 0; i < 10; i++) {
                const account = accounts[i] || {};
                const name = account.username || '';
                const password = account.password || '';
                html += '<div class="grid grid-cols-[24px_1fr_1fr] gap-2 text-xs items-center">' +
                    '<span class="text-gray-500 w-5">' + (i + 1) + '</span>' +
                    '<input type="text" class="operator-name-input w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" ' +
                    'placeholder="用户名' + (i + 1) + '" value="' + escapeAttr(name) + '" data-index="' + i + '">' +
                    '<input type="password" class="operator-password-input w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" ' +
                    'placeholder="密码" value="' + escapeAttr(password) + '" data-index="' + i + '">' +
                    '</div>';
            }
            container.innerHTML = html;
        }

        function loadOperators() {
            const accounts = readConfiguredAccounts();
            const operators = accounts.map(item => item.username).filter(Boolean);
            // 填充设置页面
            const inputs = document.querySelectorAll('.operator-name-input');
            inputs.forEach(inp => {
                const idx = parseInt(inp.dataset.index);
                if (idx >= 0 && idx < 10) {
                    inp.value = operators[idx] || '';
                }
            });
            const passwordInputs = document.querySelectorAll('.operator-password-input');
            passwordInputs.forEach(inp => {
                const idx = parseInt(inp.dataset.index);
                if (idx >= 0 && idx < 10) {
                    inp.value = accounts[idx]?.password || '';
                }
            });
            // 填充测试页面下拉框
            const select = document.getElementById('operator-input');
            if (select) {
                const currentVal = select.value;
                select.innerHTML = '<option value="">-- 选择操作人员 --</option>';
                operators.forEach(name => {
                    if (name && name.trim()) {
                        select.innerHTML += '<option value="' + escapeAttr(name.trim()) + '">' + escapeAttr(name.trim()) + '</option>';
                    }
                });
                if (currentVal) select.value = currentVal;
            }
            const settingsLoginSelect = document.getElementById('settings-login-user');
            if (settingsLoginSelect) {
                const currentLoginValue = settingsLoginSelect.value;
                settingsLoginSelect.innerHTML = '<option value="">-- 选择用户名 --</option>';
                operators.forEach(name => {
                    settingsLoginSelect.innerHTML += '<option value="' + escapeAttr(name) + '">' + escapeAttr(name) + '</option>';
                });
                if (loggedInUser && operators.includes(loggedInUser)) {
                    settingsLoginSelect.value = loggedInUser;
                } else if (currentLoginValue) {
                    settingsLoginSelect.value = currentLoginValue;
                }
            }
            if (loggedInUser && !operators.includes(loggedInUser)) {
                loggedInUser = '';
                localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            }
            applyLoginUiState();
            applySettingsAccessState();
            return operators;
        }

        function saveOperators() {
            const nameInputs = document.querySelectorAll('.operator-name-input');
            const passwordInputs = document.querySelectorAll('.operator-password-input');
            const accounts = [];
            nameInputs.forEach((inp, idx) => {
                const userName = inp.value.trim();
                const password = passwordInputs[idx] ? passwordInputs[idx].value : '';
                if (userName) {
                    accounts.push({ username: userName, password: password });
                }
            });
            localStorage.setItem(OPERATOR_ACCOUNTS_STORAGE_KEY, JSON.stringify(accounts));
            localStorage.setItem('ateq_operators', JSON.stringify(accounts.map(item => item.username)));
            loadOperators();
            return accounts;
        }
        
        // 创建产品行
        function createProductRow(product = null, index = null) {
            const idx = index !== null ? index : productCounter++;
            const model = product ? product.model : '';
            const program1 = product ? product.program1 : '';
            const program2 = product ? product.program2 : '';
            const switchChamber = product ? (product.switchChamber ? 'true' : 'false') : 'false';
            const labelTemplate = product ? product.labelTemplate : '';
            const supplierCode = product ? (product.supplierCode || '') : '';
            
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-gray-700/30 product-row';
            tr.dataset.index = idx;
            tr.innerHTML = `
                <td class="py-2 px-3 border border-gray-600 text-center">${idx + 1}</td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="text" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(model)}" placeholder="产品型号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="number" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(program1)}" placeholder="程序号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="number" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(program2)}" placeholder="程序号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <select class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm">
                        <option value="true" ${switchChamber === 'true' ? 'selected' : ''}>是</option>
                        <option value="false" ${switchChamber === 'false' ? 'selected' : ''}>否</option>
                    </select>
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <div class="flex gap-1">
                        <input type="text" class="label-template-input flex-1 min-w-[180px] px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(labelTemplate)}" placeholder="可选，选择*.btw文件">
                        <input type="file" class="label-template-file" accept=".btw" style="display:none">
                        <button type="button" class="btn-select-template px-2 py-1 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 text-white rounded text-xs whitespace-nowrap">选择</button>
                    </div>
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="text" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(supplierCode)}" placeholder="供应商代码">
                </td>
                <td class="py-2 px-3 border border-gray-600 text-center">
                    <button class="px-2 py-1 bg-red-600 hover:bg-red-500 text-white rounded text-xs delete-product" title="删除">
                        <svg class="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                        </svg>
                    </button>
                </td>
            `;

            const templateButton = tr.querySelector('.btn-select-template');
            const templatePathInput = tr.querySelector('.label-template-input');
            const templateFileInput = tr.querySelector('.label-template-file');

            function openLabelTemplatePicker() {
                templateFileInput.value = '';
                templateFileInput.click();
            }

            async function uploadLabelTemplate() {
                const file = templateFileInput.files && templateFileInput.files[0];
                if (!file) return;
                if (!file.name.toLowerCase().endsWith('.btw')) {
                    showNotification('请选择 .btw 标签模板文件', 'error');
                    return;
                }

                const oldText = templateButton.textContent;
                templateButton.disabled = true;
                templateButton.textContent = '上传中...';
                templatePathInput.disabled = true;
                try {
                    const response = await fetch('/api/upload_label_template?filename=' + encodeURIComponent(file.name), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/octet-stream' },
                        body: file
                    });
                    const result = await response.json();
                    if (result.success && result.path) {
                        templatePathInput.value = result.path;
                        showNotification('已选择标签模板', 'success');
                    } else {
                        showNotification('选择标签模板失败: ' + (result.message || ''), 'error');
                    }
                } catch (e) {
                    showNotification('选择标签模板异常: ' + e.message, 'error');
                } finally {
                    templatePathInput.disabled = false;
                    templateButton.textContent = oldText;
                    templateButton.disabled = false;
                }
            }

            templateButton.addEventListener('click', openLabelTemplatePicker);
            templatePathInput.addEventListener('dblclick', openLabelTemplatePicker);
            templateFileInput.addEventListener('change', uploadLabelTemplate);
            
            // 绑定删除按钮事件
            tr.querySelector('.delete-product').addEventListener('click', function() {
                if (document.querySelectorAll('.product-row').length > 1) {
                    tr.remove();
                    updateRowNumbers();
                } else {
                    alert('至少保留一个产品设置');
                }
            });
            
            return tr;
        }
        
        // 更新行号
        function updateRowNumbers() {
            const rows = document.querySelectorAll('.product-row');
            rows.forEach((row, index) => {
                row.querySelector('td:first-child').textContent = index + 1;
                row.dataset.index = index;
            });
            productCounter = rows.length;
        }
        
        // 添加产品
        document.getElementById('btn-add-product').addEventListener('click', function() {
            if (!requireSettingsLogin()) {
                return;
            }
            const tbody = document.getElementById('product-tbody');
            const newRow = createProductRow();
            tbody.appendChild(newRow);
            updateRowNumbers();
            
            // 滚动到新添加的行
            newRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
        
        // 保存设置
        document.getElementById('btn-save-settings').addEventListener('click', function() {
            if (!requireSettingsLogin()) {
                return;
            }
            const rows = document.querySelectorAll('.product-row');
            const products = [];
            saveOperators();

            rows.forEach((row, index) => {
                const inputs = row.querySelectorAll('input[type="text"], input[type="number"], select');
                products.push({
                    model: inputs[0].value.trim(),
                    program1: inputs[1].value,
                    program2: inputs[2].value,
                    switchChamber: inputs[3].value === 'true',
                    labelTemplate: inputs[4].value.trim(),
                    supplierCode: inputs[5].value.trim()
                });
            });
            
            localStorage.setItem('ateq_products', JSON.stringify(products));
            // Saving edits only refreshes the UI. Device synchronization is
            // deferred until an explicit product selection or test start.
            suppressProductAteqSyncOnce =
                localStorage.getItem('selected_product_index') !== null;
            loadProductSelector();
            alert('设置已保存！共 ' + products.length + ' 个产品');
        });
        
        // 加载设置
        function loadSettings() {
            renderOperatorList();
            loadOperators();
            const saved = localStorage.getItem('ateq_products');
            const tbody = document.getElementById('product-tbody');
            tbody.innerHTML = '';
            productCounter = 0;
            
            if (saved) {
                try {
                    const products = JSON.parse(saved);
                    if (products && products.length > 0) {
                        products.forEach((product, index) => {
                            const row = createProductRow(product, index);
                            tbody.appendChild(row);
                        });
                        productCounter = products.length;
                    } else {
                        // 没有数据时添加一个空行
                        tbody.appendChild(createProductRow(null, 0));
                    }
                } catch(e) {
                    console.error('加载设置失败:', e);
                    tbody.appendChild(createProductRow(null, 0));
                }
            } else {
                // 首次使用时添加一个默认产品
                const defaultProduct = {
                    model: 'MODEL001',
                    program1: '1',
                    program2: '2',
                    switchChamber: true,
                    labelTemplate: '',
                    supplierCode: ''
                };
                tbody.appendChild(createProductRow(defaultProduct, 0));
            }
            applySettingsAccessState();
        }
        

        // --- Auto-sync context for hardware-triggered tests ---
        function syncTestContext() {
            var ctx = {
                product_model: (currentProductConfig && currentProductConfig.model) || "",
                operator: getActiveOperatorName(),
                qr_code: document.getElementById('qr-input')?.value || "",
                label_template: (currentProductConfig && currentProductConfig.labelTemplate) || "",
                supplier_code: (currentProductConfig && currentProductConfig.supplierCode) || "",
                program1: (currentProductConfig && currentProductConfig.program1) || 0,
                program2: (currentProductConfig && currentProductConfig.program2) || 0,
                switch_chamber: !!(currentProductConfig && currentProductConfig.switchChamber),
            };
            return fetch("/api/sync_test_context", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(ctx),
            }).catch(function(e){console.log("syncTestContext error:",e);});
        }
        var qrEl = document.getElementById("qr-input");
        if (qrEl) {
            qrEl.readOnly = false;
            qrEl.addEventListener("input", syncTestContext);
            qrEl.addEventListener("change", handleKeyboardScan);
            qrEl.addEventListener("keydown", function(event) {
                if (event.key === "Enter") {
                    event.preventDefault();
                    handleKeyboardScan();
                }
            });
        }
        var opEl = document.getElementById("operator-input");
        if (opEl) opEl.addEventListener("change", syncTestContext);
        setTimeout(syncTestContext, 1000);

    </script>
</body>
</html>
'''

@app.get("/", response_class=HTMLResponse)
def get_ui():
    return HTMLResponse(
        content=HTML_TEMPLATE,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.post("/api/start")
def start_instrument():
    try:
        script_path = os.path.join(os.path.dirname(__file__), 'start_ateq.py')
        subprocess.Popen(['python3', script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"success": True}
    except:
        return {"success": False}

@app.post("/api/stop")
def stop_instrument():
    try:
        script_path = os.path.join(os.path.dirname(__file__), 'reset_ateq.py')
        subprocess.Popen(['python3', script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"success": True}
    except:
        return {"success": False}

@app.post("/api/select_program/{program_number}")
def api_select_program(program_number: int):
    """选择程序号"""
    try:
        ok, msg = write_program(program_number)
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/current_program")
def api_get_current_program():
    """获取当前程序号"""
    try:
        program = read_current_program()
        return {"success": True, "program": program}
    except Exception as e:
        return {"success": False, "program": None, "message": str(e)}


@app.get("/api/line_settings")
def api_get_line_settings():
    """Get scan/printer switches."""
    try:
        if get_line_settings is None:
            return {"success": False, "message": str(LINE_RUNTIME_IMPORT_ERROR)}
        return {"success": True, "settings": get_line_settings()}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/line_settings")
def api_update_line_settings(data: dict):
    """Update scan/printer switches."""
    try:
        if update_line_settings is None:
            return {"success": False, "message": str(LINE_RUNTIME_IMPORT_ERROR)}

        scan_required = data.get("scan_required") if "scan_required" in data else None
        printer_enabled = data.get("printer_enabled") if "printer_enabled" in data else None
        settings = update_line_settings(
            scan_required=scan_required,
            printer_enabled=printer_enabled,
        )
        return {"success": True, "settings": settings}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/scanner/status")
def api_scanner_status():
    """Return latest COM scanner state."""
    try:
        if SCANNER_INPUT_MODE == "keyboard":
            return {
                "success": True,
                "connected": True,
                "mode": "keyboard",
                "message": "keyboard scanner mode",
                "code": "",
                "seq": 0,
            }
        if get_scanner_state is None:
            return {"success": False, "message": f"serial_scanner import failed: {SERIAL_SCANNER_IMPORT_ERROR}"}
        state = get_scanner_state()
        return {"success": True, **state}
    except Exception as e:
        return {"success": False, "message": str(e)}


def _parse_switch_chamber(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是"}


def _start_shared_test_sequence(
    program1: int,
    program2: int = 0,
    switch_chamber: bool = False,
    product_model: str = "",
    operator: str = "",
    qr_code: str = "",
    label_template: str = "",
    supplier_code: str = "",
):
    """Arm the passive StepCode monitor, then pulse the ATEQ start coil."""
    program1 = int(program1 or 0)
    program2 = int(program2 or 0)
    switch_chamber = bool(switch_chamber)
    if program1 <= 0:
        return False, "程序1必须是大于0的有效程序号"

    if test_executor.running or getattr(test_executor, "hw_running", False):
        return False, "测试正在执行中，请勿重复启动"

    if not reset_device():
        return False, "ATEQ复位指令发送失败"
    time.sleep(0.5)
    selected, select_message = write_program(program1)
    if not selected:
        return False, f"选择程序1失败: {select_message}"
    selected_program = read_current_program()
    if selected_program != program1:
        return False, f"程序1校验失败: 期望{program1}，实际{selected_program}"

    test_executor.set_pending_test_context(
        product_model=product_model,
        operator=operator,
        qr_code=qr_code,
        label_template=label_template,
        supplier_code=supplier_code,
        program1=program1,
        program2=program2,
        switch_chamber=switch_chamber,
        arm_hardware_cycle=True,
    )

    # The cycle detector requires the exact 65535 -> 4 edge. Give its
    # 300 ms background poll one full interval to observe the idle value.
    time.sleep(0.4)
    if not start_test():
        return False, "ATEQ启动指令发送失败"
    return True, "ATEQ已启动，等待StepCode由65535变为4"


@app.post("/api/scan_qualified")
def api_scan_qualified(data: dict):
    """Store a validated scan and arm passive StepCode cycle detection."""
    started_at = time.perf_counter()
    try:
        if mark_scan_qualified is None:
            return {"success": False, "message": f"line_runtime import failed: {LINE_RUNTIME_IMPORT_ERROR}"}
        qr_code = str(data.get("qr_code") or "").strip()
        program1 = int(data.get("program1") or 0)
        program2 = int(data.get("program2") or 0)
        switch_chamber = _parse_switch_chamber(data.get("switch_chamber"))
        logger.info(
            "Scan auto-start request: qr=%s, program1=%s, program2=%s, switch_chamber=%s",
            qr_code,
            program1,
            program2,
            switch_chamber,
        )
        if not qr_code:
            logger.warning("Scan auto-start rejected: empty QR code")
            return {"success": False, "message": "二维码不能为空"}
        if program1 <= 0:
            logger.warning("Scan auto-start rejected: invalid program1=%s", program1)
            return {"success": False, "message": "请先选择包含有效程序1的产品"}
        if test_executor.running or getattr(test_executor, "hw_running", False):
            logger.warning("Scan auto-start rejected: test executor is running")
            return {"success": False, "message": "测试正在执行中，请勿重复扫码启动"}

        selected, select_message = write_program(program1)
        if not selected:
            logger.warning("Scan rejected: program1 selection failed: %s", select_message)
            return {"success": False, "message": f"选择程序1失败: {select_message}"}
        selected_program = read_current_program()
        if selected_program != program1:
            logger.warning(
                "Scan rejected: program1 verify failed, expected=%s actual=%s",
                program1,
                selected_program,
            )
            return {
                "success": False,
                "message": f"程序1校验失败: 期望{program1}，实际{selected_program}",
            }

        test_executor.set_pending_test_context(
            product_model=str(data.get("product_model") or ""),
            operator=str(data.get("operator") or ""),
            qr_code=qr_code,
            label_template=str(data.get("label_template") or ""),
            supplier_code=str(data.get("supplier_code") or ""),
            program1=program1,
            program2=program2,
            switch_chamber=switch_chamber,
            arm_hardware_cycle=True,
        )
        result = mark_scan_qualified(qr_code)
        logger.info(
            "Scan accepted for StepCode edge detection: qr=%s, elapsed_ms=%.1f",
            qr_code,
            (time.perf_counter() - started_at) * 1000,
        )

        return {
            "success": True,
            "qr_code": qr_code,
            "scanner_mode": SCANNER_INPUT_MODE,
            "test_started": False,
            "stepcode_armed": True,
            "start_message": "二维码已录入，等待ATEQ StepCode由65535变为4，或点击启动",
            "trigger_mode": "stepcode_edge",
            "program1": program1,
            "program2": program2 if switch_chamber and program2 > 0 else 0,
            "switch_chamber": switch_chamber,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            **result,
        }
    except Exception as e:
        logger.exception("Scan auto-start failed")
        return {"success": False, "message": str(e)}

@app.post("/api/start_test_sequence")
def api_start_test_sequence(
    program1: int,
    program2: int = 0,
    switch_chamber: bool = False,
    product_model: str = "",
    operator: str = "",
    qr_code: str = "",
    label_template: str = "",
    supplier_code: str = "",
):
    """启动测试序列"""
    try:
        ok, msg = _start_shared_test_sequence(
            program1=program1,
            program2=program2,
            switch_chamber=switch_chamber,
            product_model=product_model,
            operator=operator,
            qr_code=qr_code,
            label_template=label_template,
            supplier_code=supplier_code,
        )
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/sync_test_context")
async def sync_test_context(request: Request):
    """Sync frontend context for hardware-triggered tests."""
    try:
        data = await request.json()
        from program_selector import test_executor
        test_executor.set_pending_test_context(
            product_model=data.get("product_model"),
            operator=data.get("operator"),
            qr_code=data.get("qr_code"),
            label_template=data.get("label_template"),
            supplier_code=data.get("supplier_code", ""),
            program1=data.get("program1"),
            program2=data.get("program2"),
            switch_chamber=_parse_switch_chamber(data.get("switch_chamber")),
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/test_status")
def api_get_test_status():
    """获取测试状态（含稳定的完成状态负载，前端可据此在测试线程结束后可靠响应）"""
    try:
        state = test_executor.get_runtime_state()
        cp = state.get('completion_payload') or {}
        saved_payload = state.get('record_saved_payload') or {}
        test_data = state.get('test_data') or {}
        program1_pass = test_data.get('program1_pass', False)
        program2_pass = test_data.get('program2_pass', False)
        program1_pressure = test_data.get('pressure1')
        program1_leak = test_data.get('leak1')
        program1_pressure_unit = test_data.get('pressure1_unit')
        program1_leak_unit = test_data.get('leak1_unit')
        program1_result = test_data.get('result1')
        program2_pressure = test_data.get('pressure2')
        program2_leak = test_data.get('leak2')
        program2_pressure_unit = test_data.get('pressure2_unit')
        program2_leak_unit = test_data.get('leak2_unit')
        program2_result = test_data.get('result2')

        if program1_pressure is None:
            program1_pressure = cp.get('pressure1')
        if program1_leak is None:
            program1_leak = cp.get('leak1')
        if program1_pressure_unit is None:
            program1_pressure_unit = cp.get('pressure1_unit')
        if program1_leak_unit is None:
            program1_leak_unit = cp.get('leak1_unit')
        if program1_result is None:
            program1_result = cp.get('result1')
        if program2_pressure is None:
            program2_pressure = cp.get('pressure2')
        if program2_leak is None:
            program2_leak = cp.get('leak2')
        if program2_pressure_unit is None:
            program2_pressure_unit = cp.get('pressure2_unit')
        if program2_leak_unit is None:
            program2_leak_unit = cp.get('leak2_unit')
        if program2_result is None:
            program2_result = cp.get('result2')
        if not program1_pass and cp.get('program1_pass'):
            program1_pass = True
        if not program2_pass and cp.get('program2_pass'):
            program2_pass = True
        if program1_result is None and (program1_pressure is not None or program1_leak is not None):
            program1_result = 'PASS' if program1_pass else 'FAIL'
        if program2_result is None and (program2_pressure is not None or program2_leak is not None):
            program2_result = 'PASS' if program2_pass else 'FAIL'

        return {
            "success": True,
            "running": state.get('running', test_executor.running),
            "phase": state.get('phase', test_executor.current_phase),
            "active_slot": state.get('active_slot', state.get('current_program_slot')),
            "current_program_slot": state.get('current_program_slot', state.get('active_slot')),
            "program1_pass": program1_pass,
            "program2_pass": program2_pass,
            "program1_pressure": program1_pressure,
            "program1_leak": program1_leak,
            "program1_pressure_unit": program1_pressure_unit,
            "program1_leak_unit": program1_leak_unit,
            "program1_result": program1_result,
            "program2_pressure": program2_pressure,
            "program2_leak": program2_leak,
            "program2_pressure_unit": program2_pressure_unit,
            "program2_leak_unit": program2_leak_unit,
            "program2_result": program2_result,
            # 稳定完成负载字段
            "completion_sequence": state.get('completion_sequence', test_executor.completion_sequence),
            "record_saved_sequence": state.get('record_saved_sequence', test_executor.record_saved_sequence),
            "saved_qr_code": saved_payload.get('qr_code'),
            "record_saved_at": saved_payload.get('saved_at'),
            "overall_result": cp.get('overall_result'),
            "completion_phase": cp.get('phase'),
            "serial_number": cp.get('serial_number'),
            "completed_qr_code": cp.get('qr_code'),
            "record_saved": cp.get('record_saved'),
            "print_attempted": cp.get('print_attempted'),
            "print_success": cp.get('print_success'),
            "print_message": cp.get('print_message'),
            "state_source": state.get('state_source', 'memory'),
            "server_pid": os.getpid(),
        }
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/stop_test")
def api_stop_test():
    """停止测试"""
    try:
        test_executor.stop()
        return {"success": True, "message": "测试已停止"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# 导入实时监控模块的数据读取函数
from modbus_utils import (
    STATION_ID,
    TCP_PORT,
    WINDOWS_HOST_IP,
    modbus_crc,
    modbus_transaction,
    read_holding_registers,
    send_raw,
)
from ateq_units import ATEQ_UNIT_ABBREVIATIONS, decode_ateq_uint32


def parse_realtime_data(response_hex):
    """解析实时数据响应"""
    if not response_hex or len(response_hex) < 30:
        return None
    
    data = bytes.fromhex(response_hex)
    print(f"[DEBUG] 原始响应: {response_hex}")
    
    registers = []
    for i in range(3, 29, 2):
        registers.append((data[i] << 8) | data[i+1])
    
    print(f"[DEBUG] 寄存器值: {registers}")
    
    while len(registers) < 13:
        registers.append(0)
    
    # 解析压力值 (32位有符号整数)
    pressure_raw = ((registers[6] & 0xFF) << 24) | ((registers[6] >> 8) << 16) | ((registers[5] & 0xFF) << 8) | (registers[5] >> 8)
    if pressure_raw & 0x80000000:
        pressure_raw -= 0x100000000
    pressure = pressure_raw / 1000.0
    
    # 压力单位
    pressure_unit_code = decode_ateq_uint32(registers[7], registers[8])
    
    # 解析泄漏量值 (32位有符号整数)
    leak_raw = ((registers[10] & 0xFF) << 24) | ((registers[10] >> 8) << 16) | ((registers[9] & 0xFF) << 8) | (registers[9] >> 8)
    if leak_raw & 0x80000000:
        leak_raw -= 0x100000000
    leak = leak_raw / 1000.0
    
    # 泄漏量单位
    leak_unit_code = decode_ateq_uint32(registers[11], registers[12])
    
    # 状态位
    status = (registers[3] >> 8) | ((registers[3] & 0xFF) << 8)
    
    # 状态文本
    status_bits = []
    if status & 0x8000: status_bits.append("键盘锁定")
    if status & 0x0020: status_bits.append("循环结束")
    if status & 0x0010: status_bits.append("报警")
    if status & 0x0008: status_bits.append("参考端失败")
    if status & 0x0004: status_bits.append("测试端失败")
    if status & 0x0002: status_bits.append("测试失败")
    if status & 0x0001: status_bits.append("测试通过")
    status_text = " | ".join(status_bits) if status_bits else "待机中"
    
    # step_code 在寄存器4, 小端序转换
    step_code = (registers[4] >> 8) | ((registers[4] & 0xFF) << 8)
    print(f"[DEBUG] registers[4]={registers[4]}, step_code={step_code}")
    
    return {
        'pressure': pressure,
        'pressure_unit': ATEQ_UNIT_ABBREVIATIONS.get(pressure_unit_code, f"Unit({pressure_unit_code})"),
        'pressure_unit_code': pressure_unit_code,
        'leak': leak,
        'leak_unit': ATEQ_UNIT_ABBREVIATIONS.get(leak_unit_code, f"Unit({leak_unit_code})"),
        'leak_unit_code': leak_unit_code,
        'status': status,
        'status_text': status_text,
        'step_code': step_code,
    }

@app.get("/api/data")
def get_data():
    """获取实时数据"""
    try:
        response = read_holding_registers(0x30, 13)
        if response:
            data = parse_realtime_data(response)
            if data:
                # 调试输出
                print(f"[DEBUG] step_code={data['step_code']}, pressure={data['pressure']}, leak={data['leak']}")
                return {"success": True, **data}
        return {"success": False}
    except Exception as e:
        return {"success": False, "error": str(e)}

from pydantic import BaseModel
from datetime import datetime

class TestRecordRequest(BaseModel):
    serial_number: str = None
    qr_code: str = None
    pressure1: float
    leak1: float
    pressure2: float = None
    leak2: float = None
    pressure1_unit: str = None
    leak1_unit: str = None
    pressure2_unit: str = None
    leak2_unit: str = None
    test_result: str
    product_model: str = None
    operator: str = None


class PrintLabelRequest(BaseModel):
    product_model: str
    label_template: str = ""
    supplier_code: str = ""
    qr_code: str = ""


@app.post("/api/upload_label_template")
async def api_upload_label_template(request: Request, filename: str = ""):
    """Save a browser-selected .btw file on the target computer and return its path."""
    safe_name = Path(filename or "").name.strip()
    if not safe_name:
        return {"success": False, "message": "文件名为空"}
    if not safe_name.lower().endswith(".btw"):
        return {"success": False, "message": "请选择 .btw 标签模板文件"}

    data = await request.body()
    if not data:
        return {"success": False, "message": "标签模板文件为空"}

    target_dir = Path(r"D:\data\label_templates")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_name
        target_path.write_bytes(data)
    except Exception as exc:
        return {"success": False, "message": f"保存标签模板失败: {exc}"}

    return {"success": True, "path": str(target_path)}


@app.post("/api/select_label_template")
def api_select_label_template():
    """Open a native file picker on the target computer and return a .btw path."""
    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.Text = "选择BarTender标签模板"
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.StartPosition = "CenterScreen"
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.WindowState = "Minimized"
$owner.Add_Shown({
    $owner.WindowState = "Normal"
    $owner.Activate()
})
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "选择BarTender标签模板"
$dialog.Filter = "BarTender标签 (*.btw)|*.btw"
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
$dialog.RestoreDirectory = $true
if (Test-Path "D:\data") {
    $dialog.InitialDirectory = "D:\data"
}
$owner.Show()
$owner.TopMost = $true
$owner.Activate()
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
    $owner.Close()
    exit 0
}
$owner.Close()
exit 2
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "cancelled": True, "message": "选择标签模板超时"}
    except Exception as exc:
        return {"success": False, "cancelled": False, "message": str(exc)}

    selected_path = (result.stdout or "").strip().splitlines()
    selected_path = selected_path[-1].strip().lstrip("\ufeff") if selected_path else ""
    if result.returncode == 2:
        return {"success": False, "cancelled": True, "message": "已取消选择"}
    if result.returncode != 0:
        return {"success": False, "cancelled": False, "message": (result.stderr or result.stdout or "").strip()}
    if not selected_path:
        return {"success": False, "cancelled": True, "message": "未选择标签模板"}
    if not selected_path.lower().endswith(".btw"):
        return {"success": False, "cancelled": False, "message": "请选择 .btw 标签模板文件"}

    return {"success": True, "path": selected_path}


@app.post("/api/serial")
def generate_serial(data: dict):
    """生成序列号"""
    try:
        product_model = data.get('product_model', '').strip()
        qr_code = data.get('qr_code', '').strip()
        
        if not product_model:
            return {"success": False, "error": "产品型号不能为空"}
        
        # 生成序列号
        serial = get_generator().get_next_serial_number(product_model, qr_code)
        
        return {
            "success": True,
            "serial_number": serial.serial_number,
            "product_model": serial.product_model,
            "sequence": serial.sequence,
            "date": serial.date_str
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"生成序列号失败: {e}")
        return {"success": False, "error": f"生成序列号失败: {str(e)}"}


@app.post("/api/print_label")
def api_print_label(data: PrintLabelRequest):
    """Generate a daily product sequence and print the fixed BarTender label."""
    try:
        product_model = (data.product_model or "").strip()
        if not product_model:
            return {"success": False, "message": "产品型号不能为空"}

        serial = get_generator().get_next_serial_number(product_model, data.qr_code or "")
        sequence_code = f"{serial.sequence:05d}"

        from line_runtime import print_label_if_enabled

        print_result = print_label_if_enabled(
            template_name=data.label_template or "",
            serial_number=serial.serial_number,
            product_model=serial.product_model,
            qr_code=data.qr_code or serial.serial_number,
            supplier_code=data.supplier_code or "",
            sequence_code=sequence_code,
        )

        return {
            "success": bool(print_result.get("success")),
            "printed": bool(print_result.get("printed")),
            "message": print_result.get("message", ""),
            "serial_number": serial.serial_number,
            "sequence": serial.sequence,
            "sequence_code": sequence_code,
            "product_model": serial.product_model,
        }
    except Exception as e:
        logger.error(f"打印标签失败: {e}")
        return {"success": False, "message": str(e)}

@app.post("/api/records")
def create_record(record: TestRecordRequest):
    """保存测试记录"""
    try:
        record_id = save_test_record(
            qr_code=record.qr_code,
            pressure1=record.pressure1,
            leak1=record.leak1,
            pressure2=record.pressure2,
            leak2=record.leak2,
            pressure1_unit=record.pressure1_unit,
            leak1_unit=record.leak1_unit,
            pressure2_unit=record.pressure2_unit,
            leak2_unit=record.leak2_unit,
            test_result=record.test_result,
            product_model=record.product_model,
            operator=record.operator,
            serial_number=record.serial_number
        )
        return {"success": True, "id": record_id}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/records")
def get_records():
    """获取最新测试记录和统计"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = get_latest_records(50)  # 获取最新50条
        stats = get_statistics_by_date(today)
        return {
            "success": True,
            "records": records,
            "stats": stats
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/records/today")
def get_today_records():
    """获取当日测试记录和统计"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = get_records_by_date(today)  # 获取当日所有记录
        stats = get_statistics_by_date(today)
        return {
            "success": True,
            "records": records,
            "stats": stats
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==================== 全量查询API ====================
@app.get("/api/query/records")
def api_query_records(
    page: int = 1,
    limit: int = 20,
    start_date: str = None,
    end_date: str = None,
    product_model: str = None,
    result: str = None,
    qr_code: str = None,
    serial: str = None
):
    """多条件查询测试记录"""
    try:
        offset = (page - 1) * limit
        result = query_records(
            start_date=start_date,
            end_date=end_date,
            product_model=product_model,
            result=result,
            qr_code=qr_code,
            serial=serial,
            limit=limit,
            offset=offset
        )
        
        total_pages = (result['total'] + limit - 1) // limit  # 向上取整
        
        return {
            "success": True,
            "page": page,
            "limit": limit,
            "total": result['total'],
            "total_pages": total_pages,
            "pass_count": result['pass_count'],
            "fail_count": result['fail_count'],
            "pass_rate": result['pass_rate'],
            "records": result['records']
        }
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/query/export")
def api_export_records(
    start_date: str = None,
    end_date: str = None,
    product_model: str = None,
    result: str = None,
    qr_code: str = None,
    serial: str = None,
    limit: int = 10000,
    pressure_unit: str = None,
    leak_unit: str = None,
):
    """导出查询结果为CSV"""
    try:
        result_data = query_records(
            start_date=start_date,
            end_date=end_date,
            product_model=product_model,
            result=result,
            qr_code=qr_code,
            serial=serial,
            limit=limit,
            offset=0
        )

        valid_units = set(ATEQ_UNIT_ABBREVIATIONS.values())
        pressure_unit = pressure_unit if pressure_unit in valid_units else '--'
        leak_unit = leak_unit if leak_unit in valid_units else '--'
        if pressure_unit == '--' or leak_unit == '--':
            try:
                rt_response = read_holding_registers(0x30, 13)
                if rt_response:
                    rt_data = parse_realtime_data(rt_response)
                    if rt_data:
                        if pressure_unit == '--':
                            pressure_unit = rt_data.get('pressure_unit', '--')
                        if leak_unit == '--':
                            leak_unit = rt_data.get('leak_unit', '--')
            except Exception:
                pass

        # 生成CSV内容
        import csv
        from io import StringIO

        output = StringIO()
        writer = csv.writer(output)

        # 写入表头
        writer.writerow([
            '序号', '测试时间', 'QR码', '序列号', '产品型号',
            f'压力1({pressure_unit})', f'泄漏1({leak_unit})', f'压力2({pressure_unit})', f'泄漏2({leak_unit})', '结果', '操作员'
        ])

        # 写入数据
        for idx, r in enumerate(result_data['records'], 1):
            writer.writerow([
                idx,
                r.get('test_time', ''),
                r.get('qr_code', ''),
                r.get('serial_number', ''),
                r.get('product_model', ''),
                r.get('pressure1', ''),
                r.get('leak1', ''),
                r.get('pressure2', ''),
                r.get('leak2', ''),
                r.get('test_result', ''),
                r.get('operator', '')
            ])
        
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=test_records_{datetime.now().strftime('%Y%m%d')}.csv"}
        )
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/products")
def api_get_products():
    """获取所有产品型号"""
    try:
        models = get_all_product_models()
        return {
            "success": True,
            "products": [{"model": m} for m in models]
        }
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/params")
def get_params(program: int = 1):
    with modbus_transaction():
        return _get_params_exclusive(program)


def _get_params_exclusive(program: int = 1):
    """获取测试参数 (fill_time, stab_time, test_time) - 使用与read_program_times_final.py相同的方法"""
    try:
        # 参数标识符定义 (与read_program_times_final.py一致)
        PARAM_IDS = {
            0x0001: 'fill_time',   # Fill Time
            0x0002: 'stab_time',   # Stab Time  
            0x0003: 'test_time',   # Test Time
        }
        
        # 步骤1: 选择要读取时间参数的程序
        program_num = max(1, min(255, int(program or 1)))
        logger.info("Chart timing read started: program=%s", program_num)
        prog_val = program_num - 1
        cmd = f"{STATION_ID:02X}103004000102{prog_val & 0xFF:02X}{(prog_val >> 8) & 0xFF:02X}"
        full_cmd = cmd + modbus_crc(cmd)
        resp = send_raw(full_cmd)
        if not resp:
            raise Exception("选择程序失败")
        time.sleep(0.3)
        
        # 步骤2: 准备参数标识符列表 (小端序)
        param_ids = list(PARAM_IDS.keys())
        values = [len(param_ids)] + param_ids
        
        # 写入多个寄存器，使用小端序
        count = len(values)
        byte_count = count * 2
        data_hex = ''.join([f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}" for v in values])
        cmd = f"{STATION_ID:02X}10{0x0000:04X}{count:04X}{byte_count:02X}{data_hex}"
        full_cmd = cmd + modbus_crc(cmd)
        resp = send_raw(full_cmd)
        if not resp:
            raise Exception("准备参数列表失败")
        time.sleep(0.3)
        
        # 步骤3: 读取参数值 (每个参数占3个字)
        total_regs = 3 * len(param_ids)
        cmd = f"{STATION_ID:02X}03{0x0000:04X}{total_regs:04X}"
        full_cmd = cmd + modbus_crc(cmd)
        resp = send_raw(full_cmd)
        
        if not resp or len(resp) < 10:
            raise Exception("读取参数值失败")
        
        # 解析响应
        results = {}
        
        # 清理响应
        while len(resp) >= 2 and resp.startswith('00'):
            resp = resp[2:]
        
        if len(resp) >= 10 and resp[2:4] == '03':
            byte_count = int(resp[4:6], 16)
            data = resp[6:6 + byte_count * 2]
            
            for i in range(len(param_ids)):
                base = i * 12  # 每个参数占6字节=12个十六进制字符
                if base + 12 > len(data):
                    continue
                
                # 解析: 参数ID(2字节小端序) + 值(4字节小端序)
                param_id_word = int(data[base:base+4], 16)
                param_id = ((param_id_word & 0xFF) << 8) | ((param_id_word >> 8) & 0xFF)
                
                # 值: 4字节小端序 (字1是低16位，字2是高16位)
                word1 = int(data[base+4:base+8], 16)
                word2 = int(data[base+8:base+12], 16)
                word1_le = ((word1 & 0xFF) << 8) | ((word1 >> 8) & 0xFF)
                word2_le = ((word2 & 0xFF) << 8) | ((word2 >> 8) & 0xFF)
                
                # 组合成32位值 (单位: ms)
                value_ms = (word2_le << 16) | word1_le
                value_s = value_ms / 1000.0
                
                param_name = PARAM_IDS.get(param_id)
                if param_name:
                    results[param_name] = round(value_s, 2)
        
        if results:
            logger.info(
                "Chart timing read completed: program=%s, fill=%s, stab=%s, test=%s",
                program_num,
                results.get('fill_time', 3.0),
                results.get('stab_time', 2.0),
                results.get('test_time', 5.0),
            )
            return {
                "success": True,
                "program": program_num,
                "fill_time": results.get('fill_time', 3.0),
                "stab_time": results.get('stab_time', 2.0),
                "test_time": results.get('test_time', 5.0)
            }
        
        raise Exception("解析参数失败")
        
    except Exception as e:
        print(f"[DEBUG] 获取参数失败: {e}")
        # 出错时返回默认值
        return {
            "success": True,
            "program": int(program or 1),
            "fill_time": 3.0,
            "stab_time": 2.0,
            "test_time": 5.0
        }

# PLC控制API
from pydantic import BaseModel

class PLCCommand(BaseModel):
    address: str
    value: bool

# PLC uses Snap7 over Ethernet for Siemens S7-200 SMART on this machine.
PLC_CONFIG = {
    "backend": "snap7",
    "ip": "192.168.2.1",
    "rack": 0,
    "slot": 1,
    "signal": "M26.0",
    "address": "M26.0",
}

_PLC_STATUS_LOCK = threading.Lock()
_PLC_STATUS_CACHE = {"data": None, "updated_at": 0.0}
_PLC_STATUS_GOOD_CACHE = {"data": None, "updated_at": 0.0}
_PLC_STATUS_TTL_SECONDS = 5.0
_PLC_STATUS_GOOD_GRACE_SECONDS = 60.0


def _cache_plc_status(data: dict):
    _PLC_STATUS_CACHE["data"] = dict(data)
    _PLC_STATUS_CACHE["updated_at"] = time.time()
    if data.get("success") and data.get("connected") and data.get("quality") == 192 and not data.get("stale"):
        _PLC_STATUS_GOOD_CACHE["data"] = dict(data)
        _PLC_STATUS_GOOD_CACHE["updated_at"] = _PLC_STATUS_CACHE["updated_at"]
    return data


def _cache_plc_write_status(enabled: bool, output: str = ""):
    """Write-through cache so the UI never shows the old value for five seconds."""
    return _cache_plc_status({
        "success": True,
        "connected": True,
        "auto_manual": bool(enabled),
        "m26_0": bool(enabled),
        "source": "snap7",
        "quality": 192,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": PLC_CONFIG["backend"],
        "raw": output,
        "config": PLC_CONFIG,
        "read_mode": "write-through",
    })


def _cached_plc_status():
    cached = _PLC_STATUS_CACHE.get("data")
    if not cached:
        return None
    data = dict(cached)
    data["cached"] = True
    data["cache_age_seconds"] = round(time.time() - _PLC_STATUS_CACHE.get("updated_at", 0.0), 2)
    return data


def _recent_good_plc_status(message: str = ""):
    cached = _PLC_STATUS_GOOD_CACHE.get("data")
    if not cached:
        return None
    age = time.time() - _PLC_STATUS_GOOD_CACHE.get("updated_at", 0.0)
    if age > _PLC_STATUS_GOOD_GRACE_SECONDS:
        return None

    data = dict(cached)
    data["success"] = True
    data["connected"] = True
    data["cached"] = True
    data["stale"] = True
    data["cache_age_seconds"] = round(age, 2)
    if message:
        data["message"] = f"最近一次PLC读取正常，当前刷新失败：{message}"
    return data


def _line_runtime_ready():
    if LINE_RUNTIME_IMPORT_ERROR is not None:
        return False, f"line_runtime import failed: {LINE_RUNTIME_IMPORT_ERROR}"
    if read_m26_0 is None or set_m26_0 is None:
        return False, "line_runtime is not available"
    return True, ""


def _parse_m26_output(output: str):
    def parse_quality(line: str):
        marker = "quality="
        if marker not in line:
            return None
        text = line.split(marker, 1)[1].split(",", 1)[0].strip()
        try:
            return int(text)
        except ValueError:
            return None

    def parse_timestamp(line: str):
        marker = "timestamp="
        if marker not in line:
            return ""
        return line.split(marker, 1)[1].strip()

    reads = {"snap7": None, "s7": None, "cache": None, "device": None}
    text = str(output)
    read_matches = re.findall(
        r"READ\s+(cache|device|snap7|s7):\s*(.*?)(?=(?:\s+READ\s+(?:cache|device|snap7|s7):)|[\r\n]|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not read_matches:
        read_matches = []
        for line in text.splitlines():
            clean = line.strip()
            if clean.startswith("READ cache:"):
                read_matches.append(("cache", clean.split(":", 1)[1].strip()))
            elif clean.startswith("READ device:"):
                read_matches.append(("device", clean.split(":", 1)[1].strip()))
            elif clean.startswith("READ snap7:"):
                read_matches.append(("snap7", clean.split(":", 1)[1].strip()))
            elif clean.startswith("READ s7:"):
                read_matches.append(("s7", clean.split(":", 1)[1].strip()))

    for source, body in read_matches:
        source = source.lower()
        clean = f"READ {source}: {str(body).strip()}"
        if "bool=ON" in clean:
            value = True
        elif "bool=OFF" in clean:
            value = False
        else:
            value = None

        quality = parse_quality(clean)
        reads[source] = {
            "source": source,
            "value": value,
            "quality": quality,
            "quality_good": quality == 192,
            "timestamp": parse_timestamp(clean),
            "line": clean,
        }

    snap7_read = reads["snap7"] or reads["s7"]
    cache = reads["cache"]
    device = reads["device"]
    selected = None
    if snap7_read and snap7_read["quality_good"] and snap7_read["value"] is not None:
        selected = snap7_read
    elif cache and cache["quality_good"] and cache["value"] is not None:
        selected = cache
    elif device and device["quality_good"] and device["value"] is not None:
        selected = device

    if selected:
        return {
            "connected": True,
            "value": selected["value"],
            "source": selected["source"],
            "quality": selected["quality"],
            "timestamp": selected["timestamp"],
            "snap7": snap7_read,
            "device": device,
            "cache": cache,
            "message": "",
        }

    return {
        "connected": False,
        "value": None,
        "source": "snap7",
        "quality": snap7_read["quality"] if snap7_read else (cache["quality"] if cache else (device["quality"] if device else None)),
        "timestamp": snap7_read["timestamp"] if snap7_read else (cache["timestamp"] if cache else (device["timestamp"] if device else "")),
        "snap7": snap7_read,
        "device": device,
        "cache": cache,
        "message": "没有读到M26.0有效Snap7值或quality不是192，PLC未连接",
    }

@app.get("/api/plc/control-inputs")
def api_plc_control_inputs():
    """Read the PLC commands mapped to the screen start/stop buttons."""
    return {"success": False, "connected": False, "message": "PLC关联已取消"}
    try:
        if read_plc_control_inputs is None:
            return {"success": False, "message": str(LINE_RUNTIME_IMPORT_ERROR)}
        inputs = read_plc_control_inputs()
        return {
            "success": True,
            "connected": True,
            "m25_0_start": bool(inputs.get("start")),
            "m25_1_stop": bool(inputs.get("stop")),
            "raw": inputs.get("raw"),
            "quality": inputs.get("quality"),
            "timestamp": inputs.get("timestamp"),
        }
    except Exception as exc:
        return {"success": False, "connected": False, "message": str(exc)}


@app.post("/api/plc/command")
def api_plc_command(command: PLCCommand):
    """Keep the retired M26.0 hardware-I/O start output safely OFF."""
    return {"success": False, "connected": False, "message": "PLC关联已取消"}
    started_at = time.perf_counter()
    try:
        ready, message = _line_runtime_ready()
        if not ready:
            return {"success": False, "message": message, "config": PLC_CONFIG}

        if command.address.upper() != "M26.0":
            return {
                "success": False,
                "message": "当前 PLC 接口只开放 M26.0 扫码合格信号",
                "config": PLC_CONFIG,
            }

        if command.value:
            return {
                "success": False,
                "message": "M26.0旧硬件IO启动已停用；请使用界面启动或PLC M25.0启动",
                "connected": True,
                "m26_0": False,
                "config": PLC_CONFIG,
            }

        output = set_m26_0(False)
        _cache_plc_write_status(False, output)
        state_text = "OFF"
        return {
            "success": True,
            "message": f"M26.0 {state_text} 已通过 Snap7 发送",
            "connected": True,
            "m26_0": bool(command.value),
            "raw": output,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "config": PLC_CONFIG,
        }
    except Exception as e:
        return {"success": False, "message": str(e), "config": PLC_CONFIG}

@app.get("/api/plc/status")
def api_plc_status():
    """Read M26.0 through Snap7."""
    return {"success": False, "connected": False, "message": "PLC关联已取消"}
    cached = _cached_plc_status()
    if cached and time.time() - _PLC_STATUS_CACHE.get("updated_at", 0.0) < _PLC_STATUS_TTL_SECONDS:
        return cached

    if not _PLC_STATUS_LOCK.acquire(blocking=False):
        if cached:
            cached["message"] = (cached.get("message") or "") + "；PLC状态正在刷新"
            return cached
        return {
            "success": False,
            "connected": False,
            "message": "PLC状态正在读取，请稍后重试",
            "config": PLC_CONFIG,
            "read_mode": "cache",
        }

    try:
        ready, message = _line_runtime_ready()
        if not ready:
            return _cache_plc_status({
                "success": False,
                "connected": False,
                "message": message,
                "config": PLC_CONFIG,
                "read_mode": "cache",
            })

        output = read_m26_0()
        parsed = _parse_m26_output(output)
        if not parsed["connected"]:
            recent_good = _recent_good_plc_status(parsed["message"])
            if recent_good:
                return _cache_plc_status(recent_good)
            return _cache_plc_status({
                "success": False,
                "connected": False,
                "message": parsed["message"],
                "m26_0": None,
                "source": parsed["source"],
                "quality": parsed["quality"],
                "timestamp": parsed["timestamp"],
                "raw": output,
                "parsed": parsed,
                "backend": PLC_CONFIG["backend"],
                "config": PLC_CONFIG,
                "read_mode": "cache",
            })

        m26_0 = parsed["value"]
        return _cache_plc_status({
            "success": True,
            "connected": True,
            "auto_manual": bool(m26_0),
            "m26_0": m26_0,
            "source": parsed["source"],
            "quality": parsed["quality"],
            "timestamp": parsed["timestamp"],
            "backend": PLC_CONFIG["backend"],
            "raw": output,
            "parsed": parsed,
            "config": PLC_CONFIG,
            "read_mode": "cache",
        })
    except Exception as e:
        parsed = _parse_m26_output(str(e))
        if parsed.get("connected"):
            m26_0 = parsed["value"]
            return _cache_plc_status({
                "success": True,
                "connected": True,
                "auto_manual": bool(m26_0),
                "m26_0": m26_0,
                "source": parsed["source"],
                "quality": parsed["quality"],
                "timestamp": parsed["timestamp"],
                "backend": PLC_CONFIG["backend"],
                "raw": str(e),
                "parsed": parsed,
            "message": "PLC已读到有效值，但本次调用返回异常",
                "config": PLC_CONFIG,
                "read_mode": "cache",
            })

        recent_good = _recent_good_plc_status(str(e))
        if recent_good:
            return _cache_plc_status(recent_good)

        return _cache_plc_status({
            "success": False,
            "connected": False,
            "message": str(e),
            "config": PLC_CONFIG,
            "read_mode": "cache",
        })
    finally:
        _PLC_STATUS_LOCK.release()

if __name__ == '__main__':
    import uvicorn
    print("ATEQ 控制界面: http://localhost:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
