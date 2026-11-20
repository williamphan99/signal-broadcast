# Signal Broadcast

A local Mac app for sending a message and images to selected Signal groups. It links
as a secondary device. Your phone remains the primary Signal device.

On the Mac it installs as **Sudoku.app**: when locked it is an ordinary Sudoku game,
and the broadcast app sits behind it with a password and an encrypted vault. Source
code stays on GitHub. There is no app account, hosted backend, analytics,
password-recovery server or cloud backup feature. Everything is stored on the Mac.
The only network traffic is Signal's own servers, plus GitHub and Homebrew when you
run Update or Setup.

For Pixel/Termux instructions, see [PIXEL-SETUP.md](PIXEL-SETUP.md). The Pixel web
interface is separate and does **not** implement this Mac password protection.

## Installation and upgrades

1. Clone this repository anywhere on the Mac.
2. Double-click **Setup.command**. It copies the app into `~/Applications/Sudoku.app`
   and continues from there: it installs the dependencies, builds the native app
   and the local Keychain helper, moves any earlier installation (see below) and
   installs the background service.
3. Setup checks FileVault. If it is off, it opens **System Settings → Privacy &
   Security → FileVault**; turn it on and keep the recovery key offline.
4. Set and confirm a password of at least 12 characters. Spaces and paste work.
5. Scan the QR code from Signal on your phone under **Settings → Linked Devices**.
6. Drag **Sudoku** from `~/Applications` to the Dock.
7. Setup offers to delete the folder you cloned. Say yes: it now only holds source
   code, and its name gives the app away. A folder with uncommitted changes is kept.

**Upgrading an earlier installation.** Press **Update**, then **Finish update**; the
installer opens and does the same steps. It also moves the earlier install to
neutral names, safely and only once:

| Before | After |
|---|---|
| `~/Library/Application Support/Signal Broadcast/data.sparsebundle` | `~/Library/Application Support/Sudoku/library.sparsebundle` (volume "Sudoku Library") |
| Keychain `com.user.signal-broadcast.vault.v1` | Keychain `local.sudoku.library.v1` |
| Background service `com.user.signal-broadcast.service` | `local.sudoku.helper` |
| Dock app `Signal Broadcast.app` in the project folder | `~/Applications/Sudoku.app` |
| Unencrypted files in the project folder (older installs) | moved into the app, then into the vault at password setup |

The migration checks everything first and stops without changing anything if both
old and new data exist. It reads the old Keychain entry with the helper that last
used it, so normally no new approval is needed; if macOS asks, choose **Always Allow**.
It can simply be run again after a fix.
Your Signal link, drafts, photos, schedule and password are kept. Re-pin the app in
the Dock afterwards.

Installs from before the encrypted vault must also set a password. After password
setup, the vault migration copies the existing Signal store, drafts, group choices
and logs into the vault, verifies the encrypted copy by reopening it, then removes
the old copies. A valid link is preserved. If it cannot finish, normal access stays
blocked and the originals are retained; restore any missing attachments and unlock
again to retry.

Updates come from the same GitHub project. Code-only updates restart the app and
service; others open the installer. Active operations must finish first, and
downloaded updates pause scheduled sends until installed. Enter the password again
after restarting; no QR scan or relinking is needed.

## Sudoku front (on by default)

When the app is locked it looks and plays like an ordinary offline Sudoku game: its
window, menu bar, Dock name, icon and process name all say Sudoku.

- **Unlock (two steps, same password):** choose **Game → Enter Puzzle Code…** (or press
  Command-K) and type your password. The password screen appears; type the same
  password there. An unattended password screen returns to the game after 90 seconds,
  or press **Back to game**.
- **Six-digit codes** such as `482913` only load a puzzle. They never count as attempts.
- **Any other wrong code** counts as a wrong password, and so does a wrong password on
  the second screen. Three in a row erase everything. The game only ever says
  "Invalid puzzle code."
- **Playing never counts.** Wrong digits, Check, Hint and new games have no effect on
  the counter.
- **Panic code (optional):** set one under **Security → Sudoku disguise**. Typing it as
  a puzzle code (or as the password) erases everything immediately, while the game
  keeps showing.
- **Turn it off** under **Security → Sudoku disguise**. The app then shows the password
  screen when locked.
- **First run after updating:** the password screen appears once more, with an
  explanation. The Sudoku front switches on after that successful unlock.
- **After an erase** the app is just Sudoku. Run `Setup.command` inside the app
  (`~/Applications/Sudoku.app/Contents/Resources/app`) to set up again.
- **Lock now** (or Command-L) returns to Sudoku at any time.

The disguise only stops a casual look. The source code is public, and someone who
digs can find the background service, the `java` Signal process and the app's files.
What protects the data is the encryption, sealing and erasure described below:
finding the app gives an intruder nothing they can open.

## Locking, closing and erasing

There is **no inactivity timer**. The app stays unlocked during and after sending
until you lock it, close the window, or lock the Mac screen.

| Action | What happens |
|---|---|
| **Lock now** (Command-L) | Show Sudoku and require the password. Background work continues. Once nothing is running, protected storage is closed (sealed). |
| Close window or Cmd-Q | Close and lock the interface. Background work continues. |
| Reopen | Require the password before showing any account details or content. |
| Lock the Mac | Lock the app interface. Background work can continue while the Mac is awake. |
| Sleep | macOS pauses execution. No data is erased. |
| Mac logout, reboot or service restart | Keep encrypted data; require an initial password unlock before sending resumes. |
| **Log out and erase** | After confirmation, stop broadcasts and schedules and erase this installation's data. |
| Third consecutive wrong password or puzzle code | Automatically perform the same erasure. |
| Panic code typed as a puzzle code | Perform the same erasure at once. |
| This Mac removed from the phone's Linked Devices | Confirmed by two checks at least 10 minutes apart, then the same erasure. |

**Log out and erase** is available in Security settings, and on the password screen
when the Sudoku front is off. It does not require the password. A person who can reach this screen can deliberately
erase the installation. There is no way to recover an erased vault through this app.

Successful authentication resets the attempt count. Wrong current passwords in
**Change password** also count. Network errors, cancelled prompts and mismatched
new-password confirmations do not count. Restarting the app does not reset failures.

The old wipe-on-quit and unplug-to-wipe controls are retired. Migration removes their
old background jobs. Unplugging the Mac no longer erases data.

## Sending and schedules

- **Send**: compose a message, move images into the vault, reorder them and confirm
  the send. Locking or closing the window does not stop it.
- **Notes**: removed from the Mac app. It keeps no Note-to-Self copies: group refresh
  downloads no attachments at all, and copies kept by an earlier version are deleted
  at the next unlock (photos the saved draft still uses are kept). Your notes remain
  in Note to Self on the phone.
- **Groups**: search, toggle groups, select/deselect visible matches and save.
  Selections outside the current search are retained.
- **Schedule**: save daily times, see the next time or pending run, and read schedule activity.
- **Security**: change the password, lock, erase, clear logs, set the Sudoku disguise
  and panic code, or adjust sending pace.

Choose formatting beside the message. **Retry failed groups** uses the unchanged saved draft and excludes
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

Active broadcasts and group refreshes use `caffeinate` to prevent idle
sleep. This does not wake a sleeping Mac or override closing its lid. Keep it awake
for on-time schedules.
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

The vault is at `~/Library/Application Support/Sudoku/library.sparsebundle`, next to
the game's saved puzzle. It contains the Signal credentials and databases, account
number, groups, drafts, images, logs and temporary media. The installed app holds
source code and dependencies only.

The image uses AES-256 encryption. A random vault password is wrapped with AES-GCM
using an Argon2id-derived key. The wrapped material and attempt state live in the
local file-based Mac Keychain (`local.sudoku.library.v1`) without iCloud
synchronization. The user password and unwrapped vault password are not saved. The
app excludes its storage from Time Machine and its vault from Spotlight indexing.

While the app is locked and no worker is using it, the vault is **detached**: Finder
and Terminal see no decrypted files. The background service keeps the vault key in
memory only, so it can reopen the vault for a due scheduled send or a link check, then
closes it again. A send that is still waiting (for example on the cooldown) is retried
every two minutes rather than holding the vault open. The vault stays open while a
broadcast or group update runs, and detaching waits if a Finder window or file inside
it is still open. Restarting the service or the Mac forgets the key.

**Unlinking from the phone erases this Mac.** The service checks the link shortly after
each unlock, after a failed send or receive, and every six hours. When Signal rejects
this device while its server is otherwise reachable, sending is blocked and the link is
rechecked every ten minutes. After three rejections spanning at least an hour, with no
successful check in between, all local data is erased, including every photo. Network
errors, timeouts and an empty local account list never count. A Mac that is off, or
whose service has restarted and not yet been unlocked, notices at the next unlock.

**What an intruder finds.** With the app locked: a Sudoku game, an encrypted image they
cannot open, and three guesses before it erases itself. Copying the image to guess
offline only works against a weak password, so use a long passphrase. Logs live inside
the vault, hold counts, timings and error categories only (never names, numbers or
message text), and dated logs are deleted after seven days.

Keep FileVault enabled; the unlocked app warns when it is off. This source-built app is
not an OS sandbox, a hardware retry counter or a replacement for GrapheneOS security.
Modified code, a compromised Mac, restored backups and other copies remain outside its
guarantees. Erasure destroys the vault's key first, so the encrypted image cannot be
opened even if disk remnants survive, but do not treat file deletion as proof that
historical backups or local snapshots are gone.

Erasure removes this installation's credentials and app data. Unlinking from the phone
also removes it from your account; after a manual **Log out and erase**, remove it under
**Signal → Settings → Linked Devices**. Your primary account and messages on other
devices are unaffected.

## Terminal and troubleshooting

The protected terminal client prompts for the app password. Run it from
`~/Applications/Sudoku.app/Contents/Resources/app`:

```sh
.venv/bin/python mac_cli.py --dry-run
.venv/bin/python mac_cli.py --sync
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

### Send diagnostics

Every broadcast, retry and resume writes `logs/send-diagnostics.jsonl` inside the
Mac encrypted vault, regardless of the legacy `debug` setting. Each completed
attempt records a timestamp, random run ID, group position, attempt number,
duration, status and a fixed error category. It never records message text,
attachments, group names, group IDs, phone numbers or raw provider errors.
Failures and recovery transitions also appear in the Mac Recent activity feed. An `error` entry is one
attempt, which may be retried; `uncertain` means delivery was not confirmed.
The file rotates at 256 KiB, retaining one previous file. Clear logs and vault
erasure remove these files. A disk write failure is shown in Recent activity
and does not interrupt sending. Existing raw debug files are not removed by an
update; use Clear logs when no send is running if you want to delete them.

Changes apply to newly started jobs after updating and restarting the app/service.
They cannot recover failure reasons from earlier broadcasts.

### Upload throttling and paused broadcasts

Signal asking to retry later now pauses dispatch across all send workers. The app
waits automatically, shows the cause and countdown in Recent activity, then tests
recovery with one send. A successful probe restores the configured send pace and
concurrency. If no send succeeds for 15 minutes during throttling, the broadcast
pauses and keeps the unsent groups for **Resume remaining**. Requests already in
flight are allowed to finish; unconfirmed sends are never automatically repeated.
Scheduled runs cannot bypass the saved paused broadcast. Membership failures need
review and are excluded from automatic retries.

The old `max_retries` setting no longer limits throttling recovery; ordinary network
errors still receive two short retries. Diagnostics record safe error categories
and recovery transitions, never the raw provider error. See
[recovery behavior and verification](docs/SEND-THROTTLE-RECOVERY.md).
