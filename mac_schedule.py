"""Durable daily slots and one coalesced pending broadcast, in local Mac time."""
import json

import engine
from datetime import datetime, timedelta

from mac_security import atomic_json

LATE_LIMIT = timedelta(hours=1)
SLOT_FORMAT = '%Y-%m-%d %H:%M'


def read(path):
    data = json.loads(path.read_text()) if path.exists() else {'enabled': False, 'times': [], 'last': ''}
    if not isinstance(data, dict) or not isinstance(data.get('enabled'), bool):
        raise ValueError('Invalid schedule')
    times = data['times']
    if not isinstance(times, list):
        raise ValueError('Invalid schedule times')
    if times:
        engine.parse_times(times)
    for key in ('pending', 'checked', 'running'):
        if data.get(key):
            datetime.strptime(data[key], SLOT_FORMAT)
    consumed, history = data.get('consumed', []), data.get('history', [])
    if (not isinstance(consumed, list) or not all(isinstance(slot, str) for slot in consumed)
            or not isinstance(history, list) or any(not isinstance(entry, dict)
                or not all(isinstance(entry.get(key), str) for key in ('at', 'state', 'message')) for entry in history)):
        raise ValueError('Invalid schedule history')
    return data


def record(data, state, message, now, slot=None):
    entry = {'at': now.isoformat(timespec='seconds'), 'state': state, 'message': message, 'slot': slot}
    previous = data.get('history', [])
    if previous and all(previous[-1].get(key) == entry[key] for key in ('state', 'message', 'slot')):
        return None
    data['history'] = [*previous, entry][-100:]
    return entry


def advance(data, now):
    """Discover missed slots without replaying consumed minutes after a clock rollback."""
    if not data['enabled']:
        return
    current = now.replace(second=0, microsecond=0)
    checked = datetime.strptime(data['checked'], SLOT_FORMAT) if data.get('checked') else current - timedelta(minutes=1)
    start = max(min(checked, current - timedelta(minutes=1)), current - timedelta(days=1))
    consumed = set(data.get('consumed', []))
    if data.get('last'):
        consumed.add(data['last'])
    due = []
    for day in (start.date(), current.date()):
        for text in data['times']:
            hour, minute = map(int, text.split(':'))
            at = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            slot = at.strftime(SLOT_FORMAT)
            if start < at <= current and slot not in consumed:
                due.append(slot)
                consumed.add(slot)
    if due:
        latest = max(due)
        old = data.get('pending')
        data['pending'] = max(old, latest) if old else latest
        data['last'] = latest
        record(data, 'pending', 'Missed times combined into one pending send.' if old or len(due) > 1
               else 'Scheduled send is due.', now, data['pending'])
    data['consumed'] = sorted(consumed)[-10080:]
    data['checked'] = current.strftime(SLOT_FORMAT)
    pending = data.get('pending')
    if pending and now - datetime.strptime(pending, SLOT_FORMAT) > LATE_LIMIT:
        data['pending'] = None
        record(data, 'expired', 'Scheduled send expired after waiting more than one hour.', now, pending)


def save(path, data):
    atomic_json(path, data)


def append_log(directory, entry):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'schedule.jsonl'
    if path.exists() and path.stat().st_size >= 256 * 1024:
        path.replace(directory / 'schedule.previous.jsonl')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')
