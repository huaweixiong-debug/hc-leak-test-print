import sys, os
from pathlib import Path

PS = Path(r"\\100.94.24.38\ATEQ Tester\ATEQ-Print-Door\program_selector.py")
WS = Path(r"\\100.94.24.38\ATEQ Tester\ATEQ-Print-Door\webui_server.py")

ps_text = PS.read_text(encoding="utf-8", errors="replace")
ws_text = WS.read_text(encoding="utf-8", errors="replace")

print(f"PS lines: {len(ps_text.splitlines())}")
print(f"WS lines: {len(ws_text.splitlines())}")
print("Files readable OK")
