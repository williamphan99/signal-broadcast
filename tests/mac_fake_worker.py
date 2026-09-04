"""Disposable transport stand-in. Writes only beneath the test's mounted vault."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mac_security import dispatch_guard

request = json.load(sys.stdin)
root = Path(request["root"])
if "sb-mac-integration-" not in str(root):
    raise SystemExit("Only disposable integration roots are allowed")
trace = root / "test-dispatch.txt"
# A child process deliberately survives its parent unless the service kills the group.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)", str(root / "signal-cli-data")])
(root / "test-child.pid").write_text(str(child.pid))
for number in range(18000):
    with dispatch_guard(root.parents[1]):
        with trace.open("a") as stream:
            stream.write(str(number) + "\n")
    print(json.dumps({"kind": "progress", "value": {"done": number + 1, "total": 18000, "status": "sent"}}), flush=True)
    time.sleep(0.05)
