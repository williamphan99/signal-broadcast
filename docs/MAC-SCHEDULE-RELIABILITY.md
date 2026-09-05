# Mac schedule reliability

The scheduler previously checked only the current minute and consumed a due time
before preflight. Busy workers, cooldown and sleep could silently lose a scheduled
broadcast. A worker-start exception could also stop the service until another unlock.

## Current behaviour

- Only one Signal operation runs at a time. Due times are persisted even while a
  broadcast, note fetch, group sync or update occupies the service.
- Missed times combine into one pending send using the latest due time. It waits
  for the active operation and cooldown. It expires after one hour of lateness.
  Discovery considers the previous day at most; older missed times cannot create
  an unbounded backlog. Times follow the Mac's local clock. Consumed local minutes
  prevent duplicate runs when the clock moves backwards.
- Disabling or saving a schedule cancels pending work. An active broadcast continues
  until Stop is confirmed. Log out and erase stops workers and removes vault data.
- Locking the app, locking the Mac screen and closing the window preserve background
  operation. Restarting the service requires another password unlock. Pending sends
  within the lateness limit can then run; expired work is recorded and discarded.
- Active broadcasts use `caffeinate -i` to prevent idle sleep. The app does not wake
  a sleeping Mac or override a closed lid. Keep the Mac awake for on-time schedules.
- Broadcasts use the saved draft and group selection at dispatch. Recipients, saved
  draft and sending settings cannot change through the service while a broadcast runs.
- Failure to create a worker retries after one minute. Dispatch is reserved durably
  before starting a worker. If dispatch might already have occurred, the run is
  recorded for review and is never replayed automatically. Existing per-recipient
  interrupted-run protection still applies.
- Schedule read/write failures are reported without taking down the service. A
  corrupt schedule stays disabled for execution until the user saves it again.

## Status and logs

Schedule shows the next local time, pending/running work, blocking reasons and the
latest 30 history entries. The saved schedule retains 100 entries. Completed runs
record sent, failed, skipped and unconfirmed counts separately from manual sends.

`logs/schedule.jsonl` inside the vault records state transitions, times and counts.
It contains no message text, attachment names or group names. At 256 KiB it rotates
to `logs/schedule.previous.jsonl`, retaining one previous file. Repeated unchanged
waiting states do not add entries. If log writing fails, the persisted schedule
history remains the source for the UI. Existing broadcast/activity logging is unchanged.

## Verification

Final combined suite: 362 tests discovered, 356 passed, six optional hardware
integration tests skipped. Python compilation and `git diff --check` passed.

Disposable tests cover overlap, cooldown, coalescing, expiry, midnight catch-up,
locking, restart, logout, disabled schedules, startup failure, disk failure,
corrupt schedule recovery, reserved-dispatch crash recovery, recipient protection,
log rotation and scheduled completion counts. Native UI tests check pending status
and schedule history. No real Signal messages or customer vault operations are used.

```sh
SB_RUN_MAC_UI=1 SB_RUN_MAC_PHOTOS=1 SB_RUN_MAC_IPC=1 SB_RUN_MAC_PROCESSES=1 \
  .venv/bin/python -m unittest discover -s tests
```

Physical sleep/wake and APFS/Keychain/launchd integration remain separate optional
acceptance tests. Simulated clocks verify scheduler policy, not actual hardware wakeup.
