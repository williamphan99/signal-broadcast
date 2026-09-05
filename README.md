# Signal Broadcast

A local Mac app for sending a message and images to selected Signal groups. It links
as a secondary device. Your phone remains the primary Signal device.

The Mac version requires a password and stores its data in an encrypted vault on
that Mac. Source code stays on GitHub. There is no app account, hosted backend,
analytics, password-recovery server or cloud backup feature. Signal traffic and
software downloads still use Signal and the software providers' services.

For Pixel/Termux instructions, see [PIXEL-SETUP.md](PIXEL-SETUP.md). The Pixel web
interface is separate and does **not** implement this Mac password protection.

## Installation and upgrades

1. Clone this repository to a folder you will keep on the Mac.
2. Double-click **Setup.command**. It installs the dependencies, compiles the local
   Keychain helper, installs the per-user background service and builds the Dock app.
3. Set and confirm a password of at least 12 characters. Spaces and paste work.
4. Scan the QR code from Signal on your phone under **Settings → Linked Devices**.
5. Drag **Signal Broadcast.app** to the Dock.

Existing installations must also set a password. Setup does not silently encrypt or
wipe their data. After password setup, migration copies the existing Signal store,
notes, drafts, group choices and logs into the vault, verifies the encrypted copy by
reopening it, then removes the old copies. A valid link is preserved. Previously
attached original files also move into protected storage.

If migration cannot finish, normal access remains blocked and the originals needed
for recovery are retained. Restore any missing attachments and unlock again to
retry. Do not delete your old files manually while migration is incomplete.

Keep the clone in its original location. If you move it, run Setup again. Use
**Update** on the password screen or in the unlocked header, then **Finish update**. Checking
and downloading updates does not unlock the vault or sign you out. Code-only updates
restart the app and service; dependency changes open Setup. Active operations must
finish first. Downloaded updates pause scheduled sends until installation finishes.
Your Signal link, drafts, photos and schedule are kept. Enter the local password
again after restarting; no QR scan or relinking is needed. Older installations without the button need one
manual `git pull --ff-only` followed by `Setup.command` to receive it.

## Locking, closing and erasing

There is **no inactivity timer**. The app stays unlocked during and after sending
until you lock it, close the window, or lock the Mac screen.

| Action | What happens |
|---|---|
| **Lock now** | Hide the interface and require the password. Background work continues. |
| Close window or Cmd-Q | Close and lock the interface. Background work continues. |
| Reopen | Require the password before showing any account details or content. |
| Lock the Mac | Lock the app interface. Background work can continue while the Mac is awake. |
| Sleep | macOS pauses execution. No data is erased. |
| Mac logout, reboot or service restart | Keep encrypted data; require an initial password unlock before sending resumes. |
| **Log out and erase** | After confirmation, stop broadcasts and schedules and erase this installation's data. |
| Third consecutive wrong password | Automatically perform the same erasure. |

**Log out and erase** is available on the password screen and in Security settings.
It does not require the password. A person who can reach this screen can deliberately
erase the installation. There is no way to recover an erased vault through this app.

Successful authentication resets the attempt count. Wrong current passwords in
**Change password** also count. Network errors, cancelled prompts and mismatched
new-password confirmations do not count. Restarting the app does not reset failures.

The old wipe-on-quit and unplug-to-wipe controls are retired. Migration removes their
old background jobs. Unplugging the Mac no longer erases data.

## Sending and schedules

- **Send**: compose a message, move images into the vault, reorder them and confirm
  the send. Locking or closing the window does not stop it.
- **Notes**: check Signal's Note to Self, select a note and use its text and complete
  attachments as the draft. Deleting a note removes only the local copy.
  Both Notes and group refresh download attachments and save each received note
  immediately. A receive operation allows up to one hour for large files and shows
  progress every five seconds. Unrelated pending messages and their attachments can
  add to the wait. An interrupted check keeps saved notes and reports that it is
  incomplete; check again to receive the remaining queue. Notes whose attachments
  were skipped by an older version must be forwarded to Note to Self again.
  The app keeps the latest 300 unexpired notes. Debug summaries in
  `logs/notes-debug.txt` record counts, duration and completion status inside the vault.
- **Groups**: search, toggle groups, select/deselect visible matches and save.
  Selections outside the current search are retained.
- **Schedule**: save daily times, see the next time or pending run, and read schedule activity.
- **Security**: change the password, lock, erase, clear logs or adjust sending pace.

Choose formatting beside the message. Notes show the complete text before you
use or copy it. **Retry failed groups** uses the unchanged saved draft and excludes
successful, skipped and unconfirmed deliveries. Changing the saved text, photo
list or formatting disables that retry.

Photos appear as numbered thumbnails. Drag them into send order, use Move earlier
or Move later, or double-click a photo to preview it. Hover over the controls for
help. Remove photo and Clear all photos detach images from the draft; they keep
the stored files. Large albums scroll inside the photo strip.
Thumbnails and previews are decoded in memory and cleared when the interface locks.

During a broadcast, a moving indicator, elapsed time and friendly activity updates
show that work is continuing. A live count shows sends in progress and completed
groups, including when parallel sends finish out of order. Preparation and waiting
are separate from successful delivery. Stop changes to **Stopping…** while the service waits
for its workers to exit. **Broadcast stopped** appears only after confirmation.
Already-dispatched messages may still arrive. Stopping a broadcast leaves any saved
schedule enabled. Resume and Discard appear only when there is an interrupted run.

After updating to this version, run `Setup.command` once when no broadcast is running.
It builds the new native image decoder and restarts the service with the new status
reporting. No password reset or relinking is required for this update.

Importing images is a **move**, not just an attachment reference. The app explains
this before import, copies and verifies each image, then deletes its selected
original. If verification or deletion fails, it reports an incomplete import.
Historical backups and other copies of that original are not erased.

Scheduled sending works while the interface is locked or closed, provided the Mac
is awake and the background service has been unlocked once since its last start.
While another operation or cooldown blocks sending, due times wait in one durable
pending slot. Multiple missed times combine into the latest due time. It expires
one hour after that time, preventing a backlog of late broadcasts. Sleep or a service
restart can delay a send; after waking and unlocking when required, only a pending
send still within this limit can start. Disabling or resaving the schedule cancels
pending work. It does not stop an active broadcast.

Active broadcasts use `caffeinate` to prevent idle sleep. This does not wake a
sleeping Mac or override closing its lid. Keep it awake for on-time schedules.
An unfinished broadcast blocks a new scheduled broadcast until it is reviewed. **Resume remaining**
excludes messages with uncertain delivery, avoiding automatic duplicate sends.

Schedule activity shows waiting reasons, expiry and results. The vault's
`logs/schedule.jsonl` records status and counts without message text or group names.
It rotates at 256 KiB and keeps one previous file. See
[the scheduler report](docs/MAC-SCHEDULE-RELIABILITY.md) for failure and restart behaviour.

Stopping or erasing cannot recall messages already dispatched to Signal. After a
sleep, network interruption or stopped job, review uncertain outcomes before sending
those messages again.

Sending is paced to reduce rate-limit failures. Signal can still rate-limit or
restrict automated sending. Send only to groups expecting the messages.

## What is protected

The vault is at `~/Library/Application Support/Signal Broadcast/data.sparsebundle`.
It contains the Signal credentials and databases, account number, groups, notes,
drafts, images, logs and temporary media. The checkout contains source code and
installed dependencies, not the migrated account's runtime files.

The image uses AES-256 encryption. A random vault password is wrapped with AES-GCM
using an Argon2id-derived key. The wrapped material and attempt state live in the
local file-based Mac Keychain without iCloud synchronization. The user password and
unwrapped vault password are not saved. The app excludes its storage from Time
Machine and its vault from Spotlight indexing.

The background service retains decrypted access while it runs. **Lock now is an
interface lock**, not a sealed storage state. Full sealing happens when that service
stops and detaches the vault. After erasure the service restarts with a fresh process.

Keep FileVault enabled. This source-built app is not an OS sandbox, a hardware retry
counter or a replacement for GrapheneOS security. Modified code, a compromised Mac,
restored backups and other copies remain outside its guarantees. Do not treat file
deletion as proof that historical SSD remnants or backups are unrecoverable.

Erasure removes this installation's credentials and app data. To remove its entry
from your account too, use **Signal → Settings → Linked Devices** on your phone.
Your primary account and messages on other devices are unaffected.

## Terminal and troubleshooting

The protected terminal client prompts for the app password:

```sh
.venv/bin/python mac_cli.py --dry-run
.venv/bin/python mac_cli.py --sync
.venv/bin/python mac_cli.py --notes
.venv/bin/python mac_cli.py --resume
```

With no flag, it asks for confirmation before sending the saved draft. Unattended
CLI invocation is blocked; use the authenticated app's saved schedule. Old Mac
linking and group-sync helpers route through this client. The unauthenticated web
interface is disabled on Mac.

If the local service is unavailable, run Setup again. If Keychain denies access,
resolve the Mac Keychain prompt or permissions first. Such failures do not count as
wrong app passwords. The source-built Keychain helper may require a new Mac approval
after an update changes its executable.

An incomplete wipe remains blocked and can be retried with **Log out and erase**.
The app must not report erasure as complete while cleanup has failed.

## Development and verification

The Mac virtual environment needs Tk and the pinned dependency in
`requirements-macos.txt`. The Swift helpers use Apple's Security, AppKit and ImageIO APIs.

```sh
.venv/bin/python -m unittest discover -s tests
SB_RUN_MAC_UI=1 .venv/bin/python -m unittest discover -s tests -p test_mac_ui.py
SB_RUN_MAC_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -p test_mac_integration.py
SB_RUN_MAC_INTEGRATION=1 SB_LONG_IDLE_SECONDS=310 .venv/bin/python -m unittest discover -s tests -p test_mac_integration.py
```

The normal suite uses disposable files and mocked OS vault operations for policy
checks. The opt-in Mac suite creates real temporary encrypted images and uniquely
named test-only Keychain items. Its sending transport is a disposable process, not
a live Signal account. The long check waits over five minutes both during and after
a job. Real Signal delivery, physical screen-lock/sleep behaviour and Intel testing
must be reported separately from these checks.

See [the implementation and acceptance record](docs/MAC-SECURITY-VERIFICATION.md)
for coverage, actual results and outstanding hardware/account checks.

Never test erasure against a real patient's data. Never commit or share Signal
credentials, a vault, its Keychain items, or legacy runtime files.

## License

MIT. See [LICENSE](LICENSE).
