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
   git clone https://github.com/williamphan99/signal-broadcast.git ~/signal-broadcast
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

### Erasing the app's data

The **Unlink…** button (top-right) signs this Mac out of Signal and erases
everything *this app* stored locally — the link keys, signal-cli's cached
groups/contacts, the group list, the message, the schedule, and any logs. It
doesn't touch anything else on the Mac, and nothing personal is left behind. Use
it before handing back a borrowed or shared Mac. Your phone isn't affected; to also
drop this device from your account, remove it under
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

## Troubleshooting

**"Send failed" — find the reason.** The app shows a short, safe category (e.g.
*network or connection problem*, *attachment or upload problem*, *rate limited*) in the
**Send** tab, and saves the activity to a plain-text log you can reopen later:
```bash
open ~/signal-broadcast/logs/                          # the logs folder
cat  ~/signal-broadcast/logs/activity-$(date +%F).txt  # today's activity
```
(`.out`/`.err`/`.log` files have no default app — read them with `cat`, or
`open -a TextEdit <file>`. `failures-*.txt` is just group IDs for "Resend failed", not
error text, so it looks like random characters — that's normal.)

**Images fail but text sends fine.** Attachments are uploaded to Signal's CDN
(`cdn.signal.org`) — a different server than text uses — so a VPN, firewall, or
antivirus that blocks or inspects that host breaks image sends while text still works.
Check the CDN is reachable (any HTTP code = OK; a timeout or SSL error = blocked):
```bash
curl -sS -m 8 -o /dev/null -w "%{http_code}\n" https://cdn.signal.org/
```
If it fails: turn the **VPN off** (or split-tunnel `*.signal.org`) and disable any
antivirus "HTTPS/SSL scanning". JPEG/PNG are the most reliable formats; HEIC (the
iPhone default) often won't display for non-Apple recipients.

**See signal-cli's raw error** by sending a test to yourself (drop the `-a <image>`
part to test text-only):
```bash
cd ~/signal-broadcast
NUM=$(signal-cli --config ./signal-cli-data -o json listAccounts | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["number"])')
signal-cli --config ./signal-cli-data -a "$NUM" send -m "test" -a /full/path/to/image.jpg "$NUM"
```

**Check config, groups, and attachment paths parse** (sends nothing):
```bash
cd ~/signal-broadcast && python3 broadcast.py --limit 2 --dry-run
```

### Diagnostic cheat sheet

Run these from the project folder. Set `NUM` once, then use whichever you need.
Replace `+61XXXXXXXXX` only if the auto-detect line doesn't work.

```bash
cd ~/signal-broadcast

# Your linked number (used by the commands below)
NUM=$(signal-cli --config ./signal-cli-data -o json listAccounts | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["number"])')
echo "linked as: ${NUM:-<not linked>}"

# Is this device linked? (lists the linked account, or nothing)
signal-cli --config ./signal-cli-data -o json listAccounts

# How many groups does signal-cli itself know? (0 here = a sync/network issue, not the app)
signal-cli --config ./signal-cli-data -a "$NUM" -o json listGroups | python3 -c 'import sys,json; print(len(json.load(sys.stdin)), "groups")'

# Force a fresh sync from the phone, then recount (keep the phone unlocked + online)
signal-cli --config ./signal-cli-data -a "$NUM" sendSyncRequest
signal-cli --config ./signal-cli-data -a "$NUM" receive --timeout 20 >/dev/null
signal-cli --config ./signal-cli-data -a "$NUM" -o json listGroups | python3 -c 'import sys,json; print(len(json.load(sys.stdin)), "groups")'

# Can it reach Signal's servers? (any HTTP code = OK; timeout or SSL error = blocked by VPN/firewall/AV)
#   chat = text, storage = groups, cdn/cdn2 = attachments
for h in chat storage cdn cdn2; do curl -sS -m 8 -o /dev/null -w "$h: %{http_code}\n" "https://$h.signal.org/" || echo "$h: FAILED"; done

# Send a test to yourself — text only, then with an image (shows the real error)
signal-cli --config ./signal-cli-data -a "$NUM" send -m "test" "$NUM"
signal-cli --config ./signal-cli-data -a "$NUM" send -m "test" -a /full/path/to/image.jpg "$NUM"

# Dry run: confirm config + groups + attachment paths parse (sends nothing)
python3 broadcast.py --limit 2 --dry-run

# Read today's activity log (plain text); list everything in logs/
cat ~/signal-broadcast/logs/activity-$(date +%F).txt
open ~/signal-broadcast/logs/

# Versions / where things are
signal-cli --version
which signal-cli qrencode python3
```

What the results mean:
- **0 groups from `listGroups`** → the phone's sync didn't reach this Mac; check the
  `storage.signal.org` line below and keep the phone unlocked + online while syncing.
- **`storage`/`cdn` time out or SSL-error** → a **VPN, firewall, or antivirus** is
  blocking Signal's non-chat servers — that breaks group sync *and* image sends while
  text still works. Turn the VPN off (or split-tunnel `*.signal.org`) / disable HTTPS
  scanning, then re-sync with **Update list from phone**.
- **a send-to-yourself error mentioning SSL / connection / upload** → same network cause.

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
| `config.example.toml` | settings template (committed; no personal data) |
| `config.toml` | your live settings — number, pacing, schedule (created from the template on setup; not in git) |
| `message.txt` | the message body (the app saves it on Send; safe to hand-edit) |
| `attachments.txt` | image paths (managed by the app; safe to hand-edit) |
| `groups.txt` | your group list; comment out a line to skip a group |
| `scripts/make-dock-app.sh` | builds `Signal Broadcast.app` |
| `scripts/schedule.py` | generates the launchd schedule from `send_times` |
| `scripts/link-device.sh`, `scripts/pull-groups.sh` | CLI-only setup helpers |
| `signal-cli-data/` | link keys — **never commit or share this** |
| `logs/` | run logs and failure ledgers |

> `config.toml`, `signal-cli-data/`, `groups.txt`, `message.txt`, `attachments.txt`,
> `logs/`, the built `.app`, and the generated plist are all `.gitignore`d — your
> number, groups, and message never get committed. Only the placeholder
> `config.example.toml` is in git.

---

## Responsible use

This automates sending to many Signal groups, which is in tension with Signal's terms
(bulk/automated messaging). Keep it slow, prefer once a day over many times, only send
to groups that expect your messages, and don't use it to spam. You are responsible for
how you use it.

## License

[MIT](LICENSE) © William Phan. Do whatever you like with it; no warranty.
</content>
</invoke>
