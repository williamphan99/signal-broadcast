# Mac password protection with manual locking

User-approved implementation spec. This is the review source of truth; the separate
verification record describes implementation and testing, not replacement requirements.

## Summary

Remove the five-minute timer entirely. The app stays unlocked during and after sending
until the user clicks Lock now, closes the window, or locks the Mac.

Locking the interface does not interrupt broadcasts or saved schedules. Three consecutive
incorrect passwords or confirmed Log out and erase stop everything and erase local data.

Keep GitHub clone-and-run installation, mandatory passwords and local storage. Pixel
implementation remains deferred.

## User behaviour

| Action or event | Behaviour |
|---|---|
| New installation | Set a password before linking Signal. |
| Existing installation | Require password setup and encrypted migration; preserve a valid link. |
| Inactivity, including after sending | Stay unlocked. No timer. |
| Lock now | Hide sensitive information and require the password. Background sending continues. |
| Close window or Cmd-Q | Close and lock the interface. Background sending continues. |
| Reopen app | Require the password. |
| Mac screen lock | Lock the interface; background work continues while macOS permits. |
| Sleep/wake | No erasure. Handle paused operations without duplicate sending. |
| Reboot, Mac logout or service restart | Retain encrypted data; require authentication before background work resumes. |
| Third consecutive wrong password | Stop work, erase data and require fresh setup/linking. |
| Log out and erase | Confirm once, then perform the same teardown without requiring the password. |

- Put Lock now in the main interface and Log out and erase on both the lock screen and Security settings.
- Label destructive logout explicitly. Ordinary locking must never erase data.
- Show attempts remaining without revealing account details, messages or recipients.
- Successful authentication resets failures. Wrong current-password submissions during password changes also count.
- Network errors, cancelled prompts and new-password confirmation mismatches never count.
- Passwords require at least 12 characters; allow spaces and paste. Forgotten passwords require erasing and relinking.
- Replace the existing wipe-on-quit and unplug-to-wipe controls and disable their old jobs during migration.

## Background service

- Move broadcasting from the window process into one per-user launchd service.
- The service owns Signal workers, schedules, authentication, retry state and vault access.
- Use distinct states: sealed, unlocked, screen_locked, erasing, and unlinked.
- Provide controlled local operations for status, unlock, lock, password change, erasure and authorized jobs.
- On locking, revoke interface authorization and clear sensitive views and pending responses. Continue existing broadcasts and previously saved schedules.
- Require authentication for reading data, starting manual sends or changing settings. Scheduled requests may execute only saved, due jobs.
- Route GUI, CLI and scheduling through this boundary. Block unauthenticated web access and development shortcuts from reaching protected data.

## Encrypted local storage

- Move runtime data outside the checkout into ~/Library/Application Support/Signal Broadcast/.
- Store Signal keys, databases, groups, notes, drafts, attachments, logs and temporary media inside an AES-256 encrypted APFS sparse bundle.
- Generate a random vault password. Wrap it with AES-GCM using an Argon2id-derived password key and store the wrapped material and retry state in the local, nonsynchronizing Mac Keychain.
- Use a pinned cryptography library. Argon2id defaults remain 64 MiB, three passes and four lanes.
- Never persist plaintext passwords or expose secrets through process arguments, environment variables or logs. Exclude app storage from Time Machine and Spotlight.
- Keep the vault accessible while the background service runs, including while the interface is locked. Seal it when the service stops.

## Migration and attachments

- Stop legacy jobs before migrating linked, broken-link and unlinked installations with leftover data.
- Verify the encrypted copy and reopening before deleting legacy files. Interrupted migration must resume before normal use.
- Preserve the agreed attachment behaviour: explain that import moves the original into protected storage, then copy, verify and delete the selected source.
- Reject symlinks and source substitutions. Failed imports must preserve a valid copy and report incomplete cleanup.

## Erasure

- Serialize password submissions and persist failures before reporting them. Reopening or updating the repository cannot reset the counter.
- On the third failure or confirmed logout, persist an erasure marker, revoke authorization and block new dispatch immediately.
- Cancel queued jobs and terminate owned workers with bounded waits.
- Remove vault-unlocking material, detach and delete the vault, remove schedules and legacy remnants, and clear interface state.
- Resume interrupted cleanup after restart. Never report success while cleanup remains incomplete.
- Wiping works offline and affects this installation. Removing its entry from the phone's linked-device list remains separate; never delete the primary Signal account.

## Tests and acceptance

Implementation is complete only after automated tests and real Mac checks with disposable data pass:

- Leave the app idle for longer than five minutes during and after broadcasting. Verify it never locks from inactivity.
- Manually lock, close and reopen the window during sending. Verify password enforcement, continued delivery and no duplicates.
- Verify saved schedules run while locked but cannot be changed without authentication.
- Verify attempts one and two preserve background work; attempt three terminates it and wipes data.
- Test logout from the lock screen during sequential sends, parallel sends, uploads and retries.
- Verify no new dispatch after erasure begins, no surviving owned workers and no recreation of erased data.
- Test simultaneous password submissions, restart persistence, interrupted migration/wiping, failed unmounts and Keychain errors.
- Inspect temporary files, logs, process arguments and lock-screen responses for leaked content.
- Run existing regressions and disposable Signal-account checks on supported Intel and Apple silicon Macs. Report real results separately from mocked coverage.

Messages already dispatched cannot be recalled by teardown.

The interface lock leaves the background service with decrypted access. Clone-and-run
cannot guarantee protection against modified code, administrator compromise or restored
backups. Recommend FileVault and avoid claiming GrapheneOS-equivalent protection or
erasure of historical copies.
