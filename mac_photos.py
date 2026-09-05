"""Cancellable, memory-only image decoding for an unlocked Mac interface."""
import json
import subprocess
import threading
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "vendor/mac-thumbnail"


class Thumbnails:
    def __init__(self, helper=HELPER):
        self.helper = helper
        self.mutex = threading.Lock()
        self.closed = False
        self.processes = set()

    def make(self, path, size):
        with self.mutex:
            if self.closed:
                return None
            try:
                proc = subprocess.Popen([str(self.helper)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                return None
            self.processes.add(proc)
        try:
            try:
                output, _ = proc.communicate(json.dumps({"path": path, "size": size}).encode(), timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.communicate()
                return None
            with self.mutex:
                return output if not self.closed and proc.returncode == 0 else None
        finally:
            with self.mutex:
                self.processes.discard(proc)

    def close(self):
        with self.mutex:
            self.closed = True
            for proc in self.processes:
                proc.kill()
