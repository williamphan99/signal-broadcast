"""Run the real Mac worker and broadcast engine against a local JSON-RPC peer."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine
import mac_worker

request = json.load(sys.stdin)
if "sb-mac-integration-" not in request["root"]:
    raise SystemExit("Disposable roots only")
engine.signal_cli_bin = lambda: sys.executable
engine._cli = lambda binary, *args: [sys.executable, str(Path(__file__).with_name("mac_fake_signal.py")), *args]
engine._unsendable_groups_unlocked = lambda account: set()
engine.check_signal_reachable = lambda: None
engine._signal_env = lambda binary: None
engine.MIN_DELAY_S = 0
try:
    mac_worker.run(request)
    mac_worker.emit("done", True)
except Exception as exc:
    mac_worker.emit("error", type(exc).__name__)
    raise SystemExit(1)
