# Mac security implementation and acceptance

Implemented for the source-built Mac app. Pixel protection remains deferred.
No live Signal account or patient data was used during development testing.

## Architecture

- `mac_service.py` owns authentication, the mounted vault, schedules and worker
  process groups. The launchd service starts sealed. It exposes a private Unix
  socket, with authenticated operations for data, settings and manual sends.
- `mac_app.py` and `mac_cli.py` are clients. The Mac entry points in `gui.py` and
  `broadcast.py` route to them. The Pixel web interface refuses requests on Mac.
- `mac_security.py` owns encryption, import/migration receipts, Keychain state and
  repeatable erasure. `scripts/mac-security.swift` accesses the nonsynchronizing
  file-based Keychain and observes Mac lock, inactive-session and sleep events.
- `mac_worker.py` uses the existing broadcast engine with runtime paths inside the
  mounted encrypted store. Passwords and volume secrets never travel in argv or
  environment variables. Mac broadcast payloads use private stdin, including the
  fallback after a daemon failure. This uses signal-cli's documented
  [multi-account JSON-RPC interface](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-jsonrpc.5.adoc).

There is no inactivity timer. A screen lock revokes interface authorization while
the service retains its mounted storage and active jobs. Reopening the window needs
the password. Starting the service after a crash, reboot or logout requires an
initial unlock before any saved schedule can run.

The third wrong password and confirmed destructive logout revoke authorization,
persist erase intent and stop worker process groups. A shared dispatch lease fences
new requests against the erase marker. Already-dispatched requests cannot be
recalled. Cleanup destroys the wrapped volume key, detaches and deletes the image,
removes verified pending originals and old runtime files, then removes retry state.
Incomplete cleanup remains blocked and can be retried. The installed service exits
after erasure so launchd starts a fresh process without the previous Python heap.

The vault is an AES-256 encrypted APFS sparse bundle. AES-GCM wraps its random
password under an Argon2id-derived key with 64 MiB, three passes and four lanes.
The cryptography dependency is pinned. The image and its internal store are
excluded from Time Machine; Spotlight indexing is disabled for the vault. Core
dumps are disabled for the service, clients and worker; Java temporary and fatal-error files
are directed into the vault.

Migration verifies hashes after detaching and reopening the encrypted copy before
deleting original files. Imports save a verified protected copy and its attachment
reference before deleting the selected source. Pending source receipts survive a
restart in Keychain, allowing erasure without the forgotten password.

## Automated coverage

The normal suite runs against disposable engine directories. Policy tests use real
Argon2id/AES-GCM and filesystem operations with substituted Keychain/disk-image
adapters. Coverage includes:

- Authentication gates, minimum password length, serialized simultaneous failures,
  failure persistence, successful resets and current-password changes.
- Manual locking during work, saved schedules while locked, clock rollback and
  rejection of new manual jobs without authentication.
- Third-failure erasure, confirmed logout without a password, failed unmounts,
  Keychain errors, missing credentials and failure to persist a filesystem marker.
- Verified migration and retry, preserved link files and schedules, symlinks,
  substituted originals, interrupted imports and cleanup after restart.
- Durable pre-dispatch progress and rejection of unreadable recovery state.

Commands:

```sh
.venv/bin/python -m unittest discover -s tests
SB_RUN_MAC_UI=1 .venv/bin/python -m unittest discover -s tests -p test_mac_ui.py
SB_RUN_MAC_INTEGRATION=1 SB_LONG_IDLE_SECONDS=310 .venv/bin/python -m unittest discover -s tests -p test_mac_integration.py
```

The Mac integration suite uses real APFS encryption, native Keychain access, Unix
IPC, process groups and a temporary launchd job. Each test owns an immutable helper,
a unique test-only Keychain item and a disposable directory. Signal is replaced by
local processes. The engine test exercises sequential and parallel dispatch,
delayed responses that model uploads, and rate-limit retries. These checks do not
prove network delivery or actual upload behaviour in Signal.

## Recorded results

Host: Apple silicon, macOS 26.6.2, Python 3.14.6, 4 September 2026.

| Check | Result |
|---|---|
| Normal suite | 295 discovered, 288 passed, 7 native opt-in checks skipped. |
| Native Tk interface | Passed login, manual lock, window-close protocol, Cmd-Q callback, reopen and logout using disposable data. |
| APFS and Keychain | Passed real encrypted migration, reopening, retry persistence and erasure. |
| Background process and IPC | Passed lock/close continuity, unique simulated dispatches and owned-process teardown. |
| Broadcast engine | Passed sequential, parallel, delayed-upload and retry teardown against the local simulator; no new dispatch after the erase marker. |
| launchd | Passed crash recovery, orphan cleanup, sealed restart requiring authentication, and process restart after erasure. |
| Bundled Signal CLI | Passed offline JSON-RPC startup in an empty store, a private temporary path containing spaces, and pipe cleanup. No send or receive was requested. |
| Wall-clock inactivity | Passed 310 seconds during sending and 310 seconds after stopping; authorization remained unlocked throughout both intervals. |
| Syntax and diff checks | Python compilation, shell syntax and `git diff --check` passed. |

Earlier native runs exposed process-group cleanup edge cases and a test helper
rebuild that invalidated temporary Keychain access. The process cleanup and fixture
isolation were corrected. Failed runs are not counted as successful acceptance.

The five-test native suite finished successfully in 1080.559 seconds. A separate
two-test run verified the bundled CLI and the updated launchd teardown in 144.736
seconds. The final Tk run passed in 4.835 seconds. The launchd check appears in both
native runs; these represent six distinct native integration tests plus the Tk test.
Obsolete disposable fixtures from failed runs were cleaned up. The user's default
vault was not created, and no existing installation was migrated or erased.

## Remaining acceptance gates

These require hardware or a disposable Signal account and have not been claimed as
completed:

1. On both supported Intel and Apple silicon Macs, link a disposable Signal
   account and send uniquely numbered messages and images to disposable groups.
   Verify recipients, order, full attachment delivery and absence of duplicates.
2. Manually lock, close, Cmd-Q and reopen during an actual broadcast. Check saved
   schedules while locked and confirm that edits require authentication.
3. Physically lock the Mac screen, sleep/wake, log out and reboot. Verify the real
   OS notifications, paused operations, sealed startup and uncertain-send handling.
4. During real sequential sends, parallel sends, uploads and backoff retries,
   exercise both third-failure erasure and confirmed logout from the lock screen.
   Verify no new dispatch, surviving worker or recreated runtime files afterward.
5. Check linked, broken-link and unlinked legacy installations with disposable
   data through the actual installer and phone linking flow. Check Keychain prompts
   following an executable update.

Do not use patient data for these gates. FileVault remains recommended. A mounted
vault is accessible to the running service and the Mac user that owns it. Modified
code, administrator compromise, restored backups and historical copies are outside
the guarantees of a source-built application. This is not GrapheneOS-equivalent
protection and cannot erase messages or account data from the phone or recipients.
