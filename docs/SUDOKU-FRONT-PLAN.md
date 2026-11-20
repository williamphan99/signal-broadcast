# Sudoku front and security hardening: plan and gap report

Status: implemented on the `sudoku-front` branch, including phase 3 (neutral names,
installation as `~/Applications/Sudoku.app`, migration). Decisions taken since this
report: the unlock is two steps with the same password (puzzle code, then the password
screen); the Notes page is removed; unlink erasure needs three server-confirmed
rejections over an hour. Not done: the Argon2 memory upgrade (3.5) and the
iCloud/Photos import warning (3.4). The README is the current user-facing description.

## 1. What exists today

- **Vault.** An AES-256 encrypted APFS sparse bundle in
  `~/Library/Application Support/Signal Broadcast/`. A random 64-hex volume password
  is wrapped with AES-GCM under an Argon2id key derived from your password (64 MiB,
  t=3, 4 lanes). The wrapped key and the failure counter sit in a non-syncing Keychain
  item ([mac_security.py](../mac_security.py)).
- **Photos.** Importing copies the image into `store/imports/` inside the vault,
  verifies it, then deletes the original. Note-to-Self media is downloaded straight
  into the vault. Thumbnails are decoded in memory only. `TMPDIR` and the Java temp
  directory point inside the vault. **The vault requirement is already met.**
- **Wipe.** The third consecutive wrong password, or *Log out and erase*, stops
  workers, overwrites the Keychain record first (crypto-erase: the bundle can no
  longer be decrypted even if disk remnants survive), then deletes the bundle, legacy
  files and Keychain item. A restart resumes an interrupted wipe
  ([mac_service.py:289](../mac_service.py:289)).
- **Locking.** Locking is interface-only. The background service keeps the vault
  mounted and decrypted so schedules can run while locked.

## 2. Proposed design

### 2.1 Feature flag
- `disguise` lives in the Keychain record, not a plaintext file. The service reads it
  before unlock and reports it in `status`. It defaults to **on** when the field is
  missing, which covers existing users, fresh installs and post-wipe.
- Turning it off requires an unlocked session (Security settings).

### 2.2 The front
- A real, playable, offline Sudoku in Tk: generator, three difficulties, pencil
  marks, Check, a timer, and harmless saved progress kept **outside** the vault so it
  looks like a normal game.
- Window title, Tk app name, About text, Dock bundle name and icon all say "Sudoku".
  It shows up on launch, Lock now, window close, Mac screen lock and after a wipe.
- A panic shortcut (Cmd-L) locks back to Sudoku immediately.

### 2.3 How it unlocks (recommended default)
Sudoku apps commonly have a **"Puzzle code"** box (*Game → Enter puzzle code…*) for
loading a shared puzzle. That box is the way in:

| What is typed into Puzzle code | Result | Counts as a wrong attempt? |
|---|---|---|
| A valid puzzle code (e.g. `#482913`) | Loads that puzzle | No |
| Your vault password | Opens the app directly | No, and it resets the counter |
| Anything else | "Invalid puzzle code" | **Yes** |

- **Sudoku play never counts.** Wrong digits, mistakes, Check and New game have no
  effect on the counter. Otherwise ordinary play would wipe the vault.
- **Why not a separate secret word that then shows the password screen?** The code is
  open source, so anyone who looks will learn the mechanism. A second short word
  adds a step for the user but no real protection. It would also need its own counter
  enforced in the service, not just in the window. Using the password as the "word"
  gives one secret, one counter and one fewer screen. If you still want two steps, it
  can be built: the word gets hashed with Argon2, stored in the Keychain record, and
  checked inside `Service`.
- **Enforcement is in the service, not the window.** `unlock` already goes through
  `Service.authenticate` over the per-user socket, with the persisted counter. The
  Sudoku window only changes which field the password is typed into. Any process
  running as you that calls the socket directly still faces the same 3-strike wipe.

### 2.4 Wrong attempts and wipe
- Keep a **single counter shared** by the Puzzle code box, `mac_cli.py` and
  *Change password*.
- Recommend keeping **3**, since passwords are at least 12 characters and typos
  happen. Setting it to 2 is a one-constant change ([§5](#5-changes-that-affect-current-users)).
- When it trips, the existing erase runs and the app then shows a fresh,
  ordinary-looking Sudoku.
- Attempts-remaining can't appear on the front because it would give it away. The
  unlocked app shows "N failed attempts since your last unlock" instead.

### 2.5 Behaviour in every service state

| State | What the window shows |
|---|---|
| Fresh install (Setup run by the owner) | Setup launches the app with `--setup` and shows the normal password-creation screen. Without the flag it shows plain Sudoku. |
| Existing user after Update (not yet armed) | Normal password screen, plus a one-time explainer: "This app will now open as Sudoku. To get in, type your password into Game → Enter puzzle code." The front is armed only after one successful unlock (`front_armed` in the Keychain record). |
| Migrating | Normal password screen. This only happens during an owner-run Setup. |
| Sealed or screen-locked (armed) | Sudoku |
| Erasing / incomplete wipe | Sudoku. The service retries the wipe on every start and tick, and `Setup.command` reports it. No visible banner. |
| After a wipe | Sudoku. The Puzzle code box does nothing useful. To start again, run `Setup.command`. |
| Link broken | Only visible after unlocking (see §3.2) |

**Lock-screen controls move:**
- **Update** moves to *Sudoku → Check for updates…*, with neutral wording, and stays
  in the unlocked header.
- **Log out and erase** moves to the unlocked Security tab. Optionally, a **panic
  code** typed into Puzzle code wipes silently and keeps showing Sudoku.
- **Forgot password:** three invalid codes wipe, which is the same outcome as today.

### 2.6 Disguising the Dock app
- `make-dock-app.sh` writes `Sudoku.app`, adds an icon (`CFBundleIconFile`) and
  `--setup` handling.
- Tk is launched through Python, so the running app may still show "Python" and the
  Python icon in the Dock and menu bar. *Unverified; test first.* Possible fixes:
  - `wm iconphoto` together with setting the app name through `tk::mac`
  - a small Swift host
  - bundling with py2app

## 3. Security gaps and fail points (ranked)

### 3.1 The vault stays mounted while the app looks locked (highest)
After *Lock now*, closing the window, or a Mac screen lock, the decrypted vault stays
mounted at `…/Signal Broadcast/mounted/store`. Anyone at your logged-in Mac account
can open it in Finder or Terminal and read Signal keys, notes, photos and drafts
without touching the app at all. The Sudoku front would hide nothing from them.

**Fix: seal when idle.**
- On lock, if no job is running, detach the vault.
- If a schedule is enabled, the service keeps the volume password **in memory only**.
  It attaches the vault just for the scheduled job and detaches afterwards.
- Unlock already supplies the password, so the user sees no difference beyond about a
  second longer to open.
- Limits:
  - While a job is running, the vault is still mounted.
  - Root, or a debugger attached to the service, can still read the key from memory.
- This reverses an approved spec line ("keep the vault accessible while the
  interface is locked"), so it needs your sign-off.

### 3.2 Unlinking from the phone is never noticed, and nothing is deleted
- A `health` job exists ([mac_worker.py:84](../mac_worker.py:84)), but nothing in the
  Mac app ever starts it.
- When you remove this Mac under Signal → Linked Devices, the Mac doesn't detect it.
  Sends and receives just fail, and every note, photo, draft, group list and log stays
  in the vault.
- Even when `link_broken` is set, the current behaviour deliberately keeps data and
  asks you to "Erase and link again" ([mac_service.py:394](../mac_service.py:394)).

**Fix: detect and auto-erase.**
- Run a link check right after each unlock, after any receive or send failure, and
  every 6 hours while the service holds the key.
- Erase only on a **positive, repeated** signal: Signal's device-removed or
  "not registered" response for our own account, seen twice at least 10 minutes
  apart, with network otherwise working.
- Do **not** reuse `ACCOUNT_UNUSABLE_PATTERN` for this. It matches bare
  `401`/`403`/`authentication` text, and network or server problems must never wipe
  anything.
- Limit: a Mac that is off or sealed can't notice. It will notice at the next unlock
  or scheduled run.
- *Unverified, test first with a disposable account:*
  - which exact signal-cli output appears after a phone-side unlink
  - whether `listAccounts` (what `link_is_broken` uses) reflects a phone-side unlink
    at all, since it reads local state

### 3.3 Copying a note sends it to the system clipboard
*Copy note* ([mac_app.py:843](../mac_app.py:843)) puts plaintext on the general
pasteboard. From there it can reach clipboard managers and, through Universal
Clipboard, your iPhone. It survives locking and wiping.

**Fix:**
- Write the text with the `org.nspasteboard.ConcealedType` and `TransientType` markers
  using the Swift helper.
- On lock, wipe or quit, clear the clipboard if it still holds what the app copied
  (compare `changeCount`).

### 3.4 Originals can survive outside the app's reach
Importing deletes the selected file, but:
- If the file came from **iCloud Drive** (including Desktop and Documents sync), it
  goes to iCloud's *Recently Deleted* for 30 days and may already be on other devices.
- A picture picked from the **Photos library** is a copy. The original stays in Photos.
- **APFS / Time Machine local snapshots** made before the import still hold the
  original. *Unverified: whether snapshots also capture the sparse bundle and the old
  Keychain database after a wipe.* Exclusions keep data out of backups, but local
  snapshots cover the whole volume.

**Fix:**
- Detect iCloud paths (`~/Library/Mobile Documents`, Desktop and Documents with iCloud
  on) and the Photos library, and warn before importing.
- Document snapshot limits. Optionally offer to delete local snapshots after a wipe.

### 3.5 The wipe counter only stops guessing through the app
Someone who copies the sparse bundle and the Keychain item can guess passwords offline,
with no 3-strike limit. Only Argon2 cost and password strength slow them down.

**Fix:**
- Raise Argon2id memory to 256 MiB, which adds roughly half a second to unlock. Existing
  users are re-wrapped automatically at their next unlock.
- Nudge users towards a passphrase.
- Keep recommending FileVault.

### 3.6 The disguise can't survive a determined look
These would still reveal the app:

| Leak | Where it shows |
|---|---|
| Checkout folder name and README | wherever the repo is cloned |
| `~/Library/Application Support/Signal Broadcast/` and the volume name | Finder, `mount`, `diskutil list` |
| launchd label `com.user.signal-broadcast.service` | System Settings → Login Items, `launchctl` |
| Keychain service `com.user.signal-broadcast.vault.v1` | Keychain Access |
| A background `java` process with signal-cli arguments | Activity Monitor |
| Network traffic | to Signal servers and the GitHub repo |
| The device name on your phone | Linked Devices list |

Phase 2 renames the identifiers you control. The rest can't be hidden by an app. The
Sudoku only stops a casual look. What actually protects your data is the encryption,
sealing and wipe.

### 3.7 Smaller items
- *Log out and erase* doesn't remove the device from Signal's servers. *Unverified:*
  whether a linked device can deregister itself through signal-cli. If it can, try it
  when online, without ever blocking the local wipe.
- `mac_cli.py` prompts mention the app by name. Make the wording neutral.
- Out of scope: the README says the Pixel web UI has **no password protection**.

### 3.8 This Mac (observed, not changed)
This checkout still runs the **legacy, unencrypted** setup:
- The project folder holds `signal-cli-data/` (account keys and attachments),
  `notes.json`, `config.toml` and 119 log files in plaintext.
- `~/Library/Application Support/Signal Broadcast` doesn't exist, so no vault has been
  created.
- The old launchd job `com.user.signal-broadcast` (daily `broadcast.py` at 14:03) is
  loaded.
- The vault service isn't installed.
- Running `Setup.command` would migrate everything into the vault. I haven't touched
  anything.

## 4. Phases
1. **Hardening** (no visible change for most users): seal when idle (3.1), unlink
   detection and erase (3.2), concealed clipboard (3.3), import warnings (3.4),
   Argon2 upgrade (3.5).
2. **Sudoku front behind the flag**: game, Puzzle-code unlock in the service, rollout
   arming, moved controls, `Sudoku.app` and icon, panic shortcut.
3. **Rename on-disk identifiers** (App Support folder, volume name, Keychain service,
   launchd label) with a verified, resumable migration like the existing one.
   Kept separate because a failed migration here locks people out.

Each phase:
- adds unit tests for the counting rules in `Service` (valid puzzle code, password,
  invalid code, concurrent submissions, restart persistence)
- runs Mac UI tests and the opt-in real-vault integration suite with disposable data
- reports real-device checks (Dock name and icon, unlink detection) separately from
  mocked coverage

## 5. Changes that affect current users

| Change | What current users notice | Recommendation |
|---|---|---|
| Sudoku front on by default | After updating, the app opens as Sudoku once they've seen the explainer and unlocked once | Arm only after one explained unlock |
| Dock app renamed to `Sudoku.app` with a new icon | Their pinned "Signal Broadcast" Dock icon breaks. The bundle is only rebuilt by Setup, not by Update. | Show a "Run Setup to finish" prompt, and ask them to re-pin |
| Lock-screen controls move | Update moves to the Sudoku menu. Log out and erase and attempts-remaining are no longer on the first screen. | As in §2.5 |
| Failed attempts shown only after unlock | They can't see how many tries remain before a wipe | Accept, or show a subtle hint |
| Attempts lowered from 3 to 2 (if chosen) | One typo fewer before a wipe | Keep 3 |
| Seal vault when locked | Opening takes about 1 s longer. Finder can't open the vault while locked. Schedules keep working. | Do it. It reverses an approved spec line, so it needs sign-off. |
| Auto-erase on phone-side unlink | Unlinking (even by accident) deletes notes, drafts and photos on the Mac. Today they're kept for relinking. | Do it, with the strict detection rule in §3.2 |
| Argon2 upgrade | Unlock about 0.5 s slower | Do it |
| Clipboard handling | Copied notes may not reach clipboard managers or the iPhone, and clear on lock | Do it |
| Phase 2 renames | Folders, Login Items and Keychain entries change names after migration | Separate release |
