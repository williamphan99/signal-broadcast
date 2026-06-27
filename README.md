# Signal Broadcast

Send one message (and optional images) to all your Signal groups, slowly enough
to stay under Signal's rate limit. Runs on a Mac, linked to your phone as a
secondary device — your number is **never re-registered**, so your phone stays
logged in, exactly like Signal Desktop.

It's a small app: a window with three tabs — **Send**, **Groups**, **Schedule**.
The same engine also runs from the command line for the automatic daily schedule.

---

## Setting it up for someone (you, the installer)

You install it once on their Mac, link their phone, and put it on their Dock. After
that they only ever click one icon and type.

1. **Clone the folder onto their Mac:**
   ```bash
   git clone <repo-url> ~/signal-broadcast
   ```
   (Cloning — rather than downloading a zip — avoids macOS's "unidentified developer"
   warning. See [Distribution notes](#distribution-notes).)
2. **Double-click `Setup.command`** in that folder. It installs the requirements
   (Homebrew, signal-cli, qrencode, python-tk), builds the Dock app, and opens the
   window. First time only; it can take a few minutes and may ask for the Mac password.
3. **Link the phone:** the window shows a QR code. On the phone, open
   **Signal → Settings → Linked Devices → "+"** and scan it. The app pulls the group
   list automatically. Done.
4. **Pin it to the Dock** (see below) and hand it over.

> **Keep the folder where it is.** The Dock app remembers this folder's location. If
> you move or rename it later, just double-click `Setup.command` again to rebuild.

### Pin it to the Dock

1. Open the project folder in **Finder**.
2. Drag **`Signal Broadcast.app`** onto the Dock (the left side, with your other apps).
3. That's the daily button. Clicking it opens the window straight away — no Terminal,
   nothing to type.

If `Signal Broadcast.app` isn't there, run `Setup.command` once and it'll be built.

---

## Using it day to day (the person sending)

Click **Signal Broadcast** on the Dock. The window opens on the **Send** tab.

- **Send tab** — type the message, optionally **Add images…**, then click the big blue
  **Send to N groups** button. It asks you to confirm (showing the first line and how
  long it'll take), then sends, showing progress and a live log. Anything that fails is
  listed; **Resend failed** retries just those.
  - **Save for auto-send** stores the message for the daily schedule *without* sending
    now — use it when you've set up automatic times (see Schedule).
- **Groups tab** — tick the groups to send to; untick any to skip. Click **Save
  selection**. Your choices stick even when you **Update list from phone**.
- **Schedule tab** — turn on automatic daily sending at times you choose.

That's the whole loop: open, type, **Send**.

### Wiping the Mac clean

The **Unlink…** button (top-right) signs this Mac out of Signal and erases
*everything* stored locally — the link keys, signal-cli's cached groups/contacts,
the group list, the message, the schedule, and any logs. Nothing personal is left
behind. Use it before handing back a borrowed or shared Mac. Your phone isn't
affected; to also drop this device from your account, remove it under
**Signal → Settings → Linked Devices** on the phone.

### Station mode (wipe if unplugged)

For a Mac that lives plugged in at one spot. On the **Security** tab, **Arm station
mode**. From then on:

- The Mac must be **plugged in** to link — on battery you'll see "Plug in to continue".
- **Unplugging** the power automatically runs the full wipe above, after a **10-second
  grace** (plug back in within those 10 seconds to cancel). It only fires once the
  unplugged reading is confirmed (a few seconds of debounce), so a momentary blip
  won't trigger it.
- After a wipe you must scan the QR to **link again**.

It's enforced by a small background agent (`watcher.py`, run via launchd) that keeps
working even if the app window is closed, and holds the Mac awake while plugged so it
can notice an unplug. **Disarm** (or **Unlink…**) turns it off and removes the agent.

> This is a deterrent, not full security — someone with time and physical access can
> still image the disk. The real protection is **FileVault** (System Settings →
> Privacy & Security → turn on disk encryption). Use station mode *with* FileVault,
> not instead of it.

---

## What it does about rate limits (the important part)

Going too fast is what gets a Signal number flagged, so the defaults err slow.

- Sends are spaced `base_delay_seconds` ± `jitter_seconds` apart (default ~16s, never
  faster than 10s). **A full run to ~150 groups takes roughly 30–40 minutes** — by
  design. The confirmation dialog shows the estimate before you commit.
- A throttled send (Signal returns a rate-limit error) backs off exponentially
  (30s → 60s → 120s …, capped at 5 min) and retries up to `max_retries` times,
  honouring an explicit retry-after when Signal sends one.
- Anything that still fails is written to `logs/failures-YYYY-MM-DD.txt` and shown
  in the app — use **Resend failed** (or, on the CLI,
  `python3 broadcast.py --groups logs/failures-YYYY-MM-DD.txt`).
- **Cooldown guard:** `cooldown_hours` is the minimum gap between whole runs. A run
  triggered too soon after the last one is skipped (the app asks first). This is
  what makes multiple scheduled times per day safe from accidental double-sends.

**Keep the Mac awake during a run.** A full send takes ~30 min; if the Mac
idle-sleeps, sends stall. Keep it plugged in with the lid open. The scheduled run
wraps itself in `caffeinate` to hold the Mac awake for the duration.

All of this lives in **`config.toml`** — the one settings file:

```toml
account            = "+61XXXXXXXXX"   # set automatically when you link your phone
base_delay_seconds = 16               # raise to go slower / safer
jitter_seconds     = 6
cooldown_hours     = 1                # minimum gap between runs
max_retries        = 4
send_times         = ["12:00", "16:00"]  # times the scheduler fires (see below)
```

---

## Running it automatically (optional)

Two ways to run, with a real tradeoff:

| | App (manual) | Scheduled |
|---|---|---|
| How | open from Dock, type, **Send** | fires at your `send_times` |
| Pro | you watch it, eyeball the message first | hands-off |
| Con | you must remember | Mac must be awake; you check logs after |

**The easy way:** open the **Schedule** tab, enter your times (24-hour, e.g.
`09:00, 13:30, 17:00`), and click **Turn on**. To change times later, edit them and
click **Update times**; **Turn off** stops it. The schedule sends whatever message you
last saved with **Send** or **Save for auto-send**, so set the message first.

**What "scheduled" means on a laptop:** the Mac must be awake and logged in at each
time. Asleep → the job runs at the next wake (still sends, just late). Powered off →
that fire is skipped. You won't see live output; check `logs/`.

> Sending the same message to ~150 groups several times a day is more flag-prone
> than once a day. The pacing and cooldown are mitigations, not magic — fewer
> fire-times is the safer choice.

---

## Advanced / command line

The CLI front end is handy for testing and is what the schedule runs:
```bash
python3 broadcast.py --limit 2 --dry-run   # show what would send, send nothing
python3 broadcast.py --limit 2             # real send to the first 2 groups
caffeinate -i python3 broadcast.py         # full run
python3 broadcast.py --groups logs/failures-2026-06-26.txt   # resend failures
```
The Schedule tab manages launchd for you. To do it from the terminal instead:
`python3 scripts/schedule.py` (prints the exact `launchctl` commands). One-time setup
without the GUI: `bash scripts/link-device.sh`, then `bash scripts/pull-groups.sh`.

To rebuild the Dock app by hand (e.g. after moving the folder):
`bash scripts/make-dock-app.sh`.

---

## Distribution notes

`git clone` is the recommended way to hand this to someone, because it sidesteps
Gatekeeper: macOS only nags about "unidentified developer" on files carrying the
`com.apple.quarantine` flag, which is set on **downloaded** zips/DMGs — not on files
that arrive via `git clone`. So a cloned `.command` just runs, and the
`Signal Broadcast.app` that `Setup.command` builds locally isn't quarantined either.
If someone downloads the repo as a **zip** instead, `Setup.command` clears the
quarantine flag for them (`xattr -dr com.apple.quarantine .`).

The Dock app is built on each machine (it bakes in that Mac's folder path and Python),
so it isn't committed to the repo — `Setup.command` creates it.

---

## Files
| File | What it is |
|---|---|
| `Setup.command` | double-click once: install requirements, link phone, build the Dock app |
| `Signal Broadcast.app` | the Dock app (built locally by Setup; not in git) |
| `Signal Broadcast.command` | fallback launcher if you'd rather not use the Dock app |
| `gui.py` | the app window (Tkinter) |
| `broadcast.py` | command-line front end (used by the schedule) |
| `engine.py` | shared core: loop, pacing, retry, failure ledger |
| `watcher.py` | station-mode background agent: wipes everything if unplugged |
| `config.toml` | settings: number, pacing, cooldown, schedule times |
| `message.txt` | the message body (the app saves it on Send; safe to hand-edit) |
| `attachments.txt` | image paths (managed by the app; safe to hand-edit) |
| `groups.txt` | your group list; comment out a line to skip a group |
| `scripts/make-dock-app.sh` | builds `Signal Broadcast.app` |
| `scripts/schedule.py` | generates the launchd schedule from `send_times` |
| `scripts/link-device.sh`, `scripts/pull-groups.sh` | CLI-only setup helpers |
| `signal-cli-data/` | link keys — **never commit or share this** |
| `logs/` | run logs and failure ledgers |
</content>
</invoke>
