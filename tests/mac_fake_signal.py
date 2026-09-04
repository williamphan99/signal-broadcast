"""Test-only signal-cli JSON-RPC peer. Never connects to Signal."""
import json
import sys
import threading
import time
from pathlib import Path

data = Path(sys.argv[sys.argv.index("--config") + 1]).parent
if "sb-mac-integration-" not in str(data):
    raise SystemExit("Disposable roots only")
settings = json.loads((data / "test-transport.json").read_text())
lock = threading.Lock()
attempts = {}

def handle(request):
    if request["method"] == "version":
        response = {"result": {"version": "disposable"}}
    else:
        group = request["params"]["groupId"]
        with lock:
            attempts[group] = attempts.get(group, 0) + 1
            with (data / "test-rpc-dispatch.jsonl").open("a") as stream:
                stream.write(json.dumps({"group": group, "attempt": attempts[group]}) + "\n")
        time.sleep(settings.get("response_delay", 0.1))
        response = ({"error": {"message": "Rate limit exceeded"}}
                    if settings.get("retry") else {"result": {"timestamp": 1}})
    with lock:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], **response}), flush=True)

for line in sys.stdin:
    threading.Thread(target=handle, args=(json.loads(line),), daemon=True).start()
