"""launchd integration harness. All storage and workers must use a disposable root."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mac_service
from mac_security import Keychain, Vault

settings = json.loads(Path(sys.argv[1]).read_text())
root = Path(settings["root"])
if "sb-mac-integration-" not in str(root) or not settings["keychain"].startswith("com.user.signal-broadcast.test."):
    raise SystemExit("Disposable roots and Keychain items only")
root.joinpath("service.pid").write_text(str(os.getpid()))
original_service = mac_service.Service
original_socket = mac_service.socket_path
def spawn(argv, **kwargs):
    return subprocess.Popen([sys.executable, str(Path(__file__).with_name("mac_fake_worker.py")), *argv[1:]], **kwargs)
mac_service.ROOT = root
mac_service.Vault = lambda: Vault(root, Path(settings["project"]), Keychain(settings["keychain"], Path(settings["helper"])))
mac_service.Service = lambda vault: original_service(vault, retire=lambda _: None, spawn=spawn)
mac_service.socket_path = lambda: original_socket(root)
mac_service.main()
