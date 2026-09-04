#!/usr/bin/env python3
"""Install/reload this checkout's per-user launchd service. Called by Setup."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

project = Path(__file__).resolve().parent.parent
label = "com.user.signal-broadcast.service"
directory = Path.home() / "Library/LaunchAgents"
directory.mkdir(parents=True, exist_ok=True)
plist = directory / (label + ".plist")
value = {
    "Label": label,
    "ProgramArguments": [str(project / ".venv/bin/python"), str(project / "mac_service.py")],
    "WorkingDirectory": str(project), "RunAtLoad": True, "KeepAlive": True,
    "ThrottleInterval": 10, "Umask": 0o077,
    "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
}
with plist.open("wb") as stream:
    plistlib.dump(value, stream)
os.chmod(plist, 0o600)
subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True)
if result.returncode:
    sys.exit("Could not start the local security service. Log into the Mac desktop and run Setup again.")
print("Local security service installed. Unlock the app to start sending.")
