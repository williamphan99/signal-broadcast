"""Failed-only retries stored inside the vault, tied to the unchanged saved draft."""
import json

import engine
from mac_security import atomic_json


def save(results, message, attachments, style):
    failed = [[r.group_id, r.name] for r in results if not r.ok and not r.skipped and not r.uncertain]
    atomic_json(engine.RUNTIME_DIR / "retry.json", {
        "fingerprint": engine.message_fingerprint(message, attachments), "style": style, "groups": failed})


def groups():
    path = engine.RUNTIME_DIR / "retry.json"
    try:
        saved = json.loads(path.read_text())
        cfg = engine.load_config()
        matches = (saved["fingerprint"] == engine.message_fingerprint(engine.read_message(), engine.read_attachments())
                   and saved["style"] == cfg.message_style)
        targets = saved["groups"]
        if not isinstance(targets, list) or any(not isinstance(row, list) or len(row) != 2
                                              or not all(isinstance(v, str) for v in row) for row in targets):
            raise ValueError("Invalid retry recipients")
    except (OSError, ValueError, KeyError, TypeError):
        raise engine.BroadcastError("No readable failed-group list is available.") from None
    if not matches:
        raise engine.BroadcastError("The saved draft or formatting changed. Failed-only retry is unavailable.")
    if not targets:
        raise engine.BroadcastError("There are no failed groups to retry.")
    return [tuple(row) for row in targets]


def available_count():
    try:
        return len(groups())
    except engine.BroadcastError:
        return 0
