# Running Signal Broadcast on a Google Pixel (Termux) — POC

This is a **proof-of-concept** guide for running the *same broadcast automation* the Mac
app does, but on a Google Pixel (Android). There's no window/UI here — you drive it from
the command line, which is all the automation needs.

> **What "works" means here:** a **manual run** (open a terminal, run one command, it
> broadcasts to all your groups with the same pacing/retry/resume as the Mac) is solid.
> **Scheduled/unattended** sending is *best-effort* on Android — see
> [Scheduling](#7-optional-scheduled-sending-best-effort) for why.

---

## How it works (the mental model)

Signal allows **one primary device per number** plus several *linked* devices (that's how
Signal Desktop works). This tool runs **`signal-cli` as a linked secondary device** —
your number is **never re-registered**, and your phone stays the primary.

On the Pixel that means **two Signal "devices" live on one phone**:

```
Pixel
├─ Signal app                         ← PRIMARY (your number)
└─ Termux (a Linux terminal on Android, no root)
   └─ proot-distro Debian (a small glibc Linux)
      ├─ Java 25 + Python 3 + qrencode
      ├─ signal-cli 0.14.x            ← LINKED secondary device (same account)
      └─ this repo (engine.py + broadcast.py)
```

**Why the Debian layer?** `signal-cli` ships a native library (`libsignal`) built for
**glibc**. Android's own C library is **Bionic**, so running `signal-cli` in *bare* Termux
fails with `UnsatisfiedLinkError`. `proot-distro` gives you a real glibc Debian inside
Termux (no root), where `signal-cli` runs normally. This is the standard, well-trodden
approach.

**Why setup fetches an extra native file.** signal-cli's JVM download bundles `libsignal`
for x86_64 Linux and macOS, but **not for ARM Linux** — so on a Pixel (aarch64) it would
otherwise fail with *"Missing required native library dependency: libsignal-client"*.
`setup-termux.sh` automatically downloads the matching ARM `libsignal_jni.so` from the
[`exquo/signal-libs-build`](https://github.com/exquo/signal-libs-build) project (prebuilt
for exactly this) and slots it in. You don't have to do anything — just know that's what
the "Fetching native libsignal…" line is doing.

**Why signal-cli 0.14.x + Java 25 (not 0.13.x on Java 21)?** Signal's current linking
protocol needs 0.14.x — an older 0.13.x fails when finishing a device link with
*"Invalid ACI!"*. 0.14.x is compiled for **Java 25**, which Termux/Debian don't package, so
`setup-termux.sh` fetches a portable **Temurin JDK 25** into `vendor/` (the same runtime the
Mac uses). You don't install Java yourself — the code discovers the vendored one.

> **Responsible use:** this sends the same message to many groups, which is in tension
> with Signal's terms on bulk/automated messaging. Keep it slow (the defaults do), prefer
> once a day, only message groups that expect it, and don't spam. You're responsible for
> how you use it.

---

## Before you start

- A **Google Pixel** (or any Android phone) with the **Signal app installed and set up**
  (your number is already registered there — that's your primary).
- ~1 GB free space (Debian guest + Java ≈ a few hundred MB).
- 20–30 minutes for first-time setup.

Install these three apps **from [F-Droid](https://f-droid.org)** — *not* the Play Store
(the Play versions are outdated/abandoned):

| App | Why |
|---|---|
| **Termux** | the Linux terminal everything runs in |
| **Termux:API** | provides `termux-wake-lock` / `termux-open-url` (used for scheduling + linking) |
| **Termux:Boot** | restarts the schedule after a reboot (only needed for scheduled sends) |

---

## 1. Set up Termux and the Debian guest

Open **Termux** and run:

```bash
pkg update -y && pkg install -y proot-distro termux-api
proot-distro install debian
proot-distro login debian
```

You're now **inside the Debian guest** (the prompt usually turns into `root@localhost`).
Everything from here until [Scheduling](#7-optional-scheduled-sending-best-effort) runs
*inside* this guest.

> Prefer **Ubuntu**? `proot-distro install ubuntu` then `proot-distro login ubuntu` also
> works — Java is vendored (Temurin 25 into `vendor/`), so the distro's own Java version
> doesn't matter. Use your chosen name (`debian` or `ubuntu`) everywhere below.

## 2. Get the code and run setup

Inside the guest:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/williamphan99/signal-broadcast.git ~/signal-broadcast
cd ~/signal-broadcast
bash scripts/setup-termux.sh
```

`setup-termux.sh` installs Python, qrencode, a portable Java 25 (Temurin), and the correct
`signal-cli` (0.14.x, JVM build) into `vendor/`, seeds `config.toml`, and **smoke-tests that
signal-cli actually launches**. If that last check passes, the hard part is done.

## 3. Set your number

Edit `config.toml` and put your real number (with country code) on the `account` line:

```bash
nano config.toml      # set:  account = "+61XXXXXXXXX"
```

(Everything else can stay at defaults — the pacing is already set to stay under Signal's
rate limit.)

## 4. Link this device to your Signal account

```bash
bash scripts/link-termux.sh
```

This prints a **QR code** and the raw `sgnl://linkdevice…` link, then waits. Because the
Signal app and signal-cli are on the **same phone**, you can't scan your own screen — use
**one** of these:

- **Second screen (reliable):** copy the `sgnl://linkdevice…` link, paste it into any QR
  generator on a laptop or second phone, then in the Pixel's **Signal → Settings → Linked
  Devices → "+"** scan that QR.
- **Deep link (try it, no second device):** copy the link, then **in a separate Termux
  session on the host** (swipe from the left in Termux for a new session; you'll be
  *outside* the guest) run:
  ```bash
  termux-open-url 'sgnl://linkdevice…paste the whole link…'
  ```
  Android may hand it straight to Signal to finish linking.

When it succeeds, Signal lists a new linked device (`pixel-broadcast`).

## 5. Pull your groups

```bash
bash scripts/pull-groups.sh
```

Keep the phone unlocked and online for a few seconds while it syncs. This writes
`groups.txt` (one group per line; comment a line out with `#` to skip that group).

*(You can also do steps 4–5 from the app in the next section — the buttons for linking and
"Update from phone" are built in.)*

## 6. Use the app (mobile web UI) — recommended for daily use

For a tap-and-type experience (no commands), start the little web app and open it in the
phone's browser:

```bash
bash scripts/webui-termux.sh          # inside the guest; leave it running
```

Then open **http://127.0.0.1:8787** in Chrome on the Pixel. You get the same three tabs as
the Mac app:

- **Send** — type the message, **Add images…**, tap **Send to N groups** (it confirms and
  shows a time estimate), watch live progress, **Resend failed** if needed.
- **Groups** — tick/untick groups, **Update from phone**, **Save selection**.
- **Schedule** — set daily times, **Turn on/off**.
- **Settings** — shows your linked number and an **Unlink & erase** button.

It's **private by design**: the server listens on `127.0.0.1` only (nothing leaves the
phone), and there's no account or login — it just uses your Signal link.

### One-tap home-screen icon (so a non-technical user never sees a terminal)

Install **Termux:Widget** (F-Droid), then create a launcher script on the Termux **host**:

```bash
mkdir -p ~/.shortcuts
cat > ~/.shortcuts/"Signal Broadcast" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
proot-distro login debian -- sh -lc 'cd ~/signal-broadcast && python3 webui.py' &
sleep 4
termux-open-url http://127.0.0.1:8787
SH
chmod +x ~/.shortcuts/"Signal Broadcast"
```

Long-press the home screen → **Widgets → Termux:Widget**, and pick **Signal Broadcast**.
From then on the user just taps that icon → the browser opens the app → type → **Send**.
(Use your guest's name if it isn't `debian`.)

## 7. Send from the command line (optional / advanced)

Prefer the terminal, or testing? Set the message, then:

```bash
nano message.txt                              # type your message
python3 broadcast.py --limit 2 --dry-run      # shows what WOULD send; sends nothing
python3 broadcast.py --limit 2                # real send to just the first 2 groups
python3 broadcast.py                          # the full broadcast to all groups
```

A full run to ~150 groups takes **~30–40 minutes by design** (the pacing is what keeps
your number safe). Keep Termux in the foreground with the screen on for the first full
run so you can watch it. Anything that fails is logged; resend just those with:

```bash
python3 broadcast.py --groups logs/failures-$(date +%F).txt
```

---

## 7. Optional: scheduled sending (best-effort)

> **Read this first.** Android's **Doze** mode aggressively suspends background apps to
> save battery. Even with the steps below, unattended scheduled sends on a phone are
> **less reliable** than a manual run. If a send *must* go out, run it manually — or run
> this on an always-on Linux box instead of the phone.

Set your times in `config.toml` (`send_times = ["12:00", "16:00"]`), then **inside the
guest**:

```bash
bash scripts/schedule-termux.sh
```

It installs a cron entry per time and prints the **host-side steps you must also do**
(battery-optimization exemption + a Termux:Boot script that holds a wake lock and starts
cron). Follow those exactly, then verify by checking `logs/cron.log` after a scheduled
time with the screen off for a while.

---

## Updating

To get newer code later, run this **inside the guest**:

```bash
cd ~/signal-broadcast
bash scripts/update-termux.sh
```

It pulls the latest code, installs any new dependencies (idempotent — skips what's already
there), and stops the running web UI so the next launch loads the new code. Then restart
with `bash scripts/webui-termux.sh` (or the home-screen icon). Your **Signal link and
settings are preserved** — `signal-cli-data/`, `config.toml`, and `groups.txt` are
gitignored, so an update never makes you re-link or reconfigure.

## Troubleshooting

**`UnsatisfiedLinkError` / `libsignal` / `GLIBC` errors when running signal-cli.**
You're probably in **bare Termux**, not the Debian guest. Run `proot-distro login debian`
first, then retry. (This is the whole reason for the Debian layer.)

**`Missing required native library dependency: libsignal-client`.** The ARM `libsignal`
didn't get installed. Re-run `bash scripts/setup-termux.sh` and watch for the "Fetching
native libsignal…" / "Injected …" lines. If the download failed (no network, or a new
signal-cli version whose libsignal build isn't published yet), grab the `.so` for your
`libsignal-client-<version>.jar` and arch from
[exquo/signal-libs-build releases](https://github.com/exquo/signal-libs-build/releases)
manually. (Setup derives the version from the jar in `vendor/…/lib/`.)

**`signal-cli` won't start / "unsupported class version".** The vendored Java 25 wasn't
found or picked up (0.14.x needs Java 25). Check that `vendor/jdk-*/bin/java` exists and
`vendor/jdk-*/bin/java -version` shows **25**; if not, re-run `setup-termux.sh` to re-fetch
the Temurin JDK.

**0 groups after `pull-groups.sh`.** The phone's sync didn't arrive. Keep the phone
unlocked + online and re-run it. Check Signal's servers are reachable from the guest:
```bash
for h in chat storage cdn cdn2; do curl -sk -m 8 -o /dev/null -w "$h: %{http_code}\n" "https://$h.signal.org/"; done
```
A cert/SSL error is normal (Signal pins its own CA); only a timeout / `000` means blocked
(usually a VPN or firewall).

**See the raw error.** Add `debug = true` to `config.toml`, reproduce, then read
`logs/debug-$(date +%F).txt`. Set it back to `false` when done.

---

## What's different from the Mac version

Same engine, same pacing/retry/resume/failure-ledger, same `config.toml`. Differences:

- **GUI is a mobile web app** (opened in the browser via a one-tap icon), not a Tkinter
  window — but the same Send / Groups / Schedule tabs.
- **No Dock app / launchd / station-mode wipe** — those are macOS-only.
- **Java 25 is a vendored Temurin build** in `vendor/` (fetched by setup), rather than a
  Homebrew keg as on the Mac — but it runs the same signal-cli 0.14.x.

## Test it without a phone (Docker sandbox)

Want to see the whole thing work before touching a Pixel? On any machine with **Docker**:

**Easiest — one command (the "pnpm dev" of this project):**
```bash
bash dev.sh
```
It builds the Pixel-like environment once (~3–5 min), starts the app, and opens
**http://localhost:8787** in your browser automatically. Re-run it anytime for an instant
start (your link persists); `bash dev.sh rebuild` after code changes.

**Or, for a hands-on shell** in the sandbox (to run the individual scripts yourself):
```bash
bash scripts/pixel-sandbox.sh
```

This drops you into an interactive **aarch64 Debian** container — the same CPU arch and
glibc + Java 25 + signal-cli 0.14.x the Pixel guest uses. Run `bash scripts/setup-termux.sh`,
then `bash scripts/link-termux.sh` and watch the real linking QR render; you can even scan
it with your phone to link, pull groups, and dry-run.

**Preview the actual app GUI in your desktop browser:** inside the sandbox run
`bash scripts/webui-termux.sh`, then open **http://localhost:8787** on your computer (the
sandbox publishes that port). You'll see the real Send/Groups/Schedule interface.

It reproduces everything **except** the Android-OS specifics (Doze, Termux:Boot, the
on-phone deep-link) — those still need a real device (checklist below). Linking from the
sandbox makes a real linked device on your account; remove it afterward under Signal →
Settings → Linked Devices.

## Device test checklist (validate on the real Pixel)

These can only be confirmed on the phone itself (they were validated in code + a Linux
sandbox, but Android specifics need the device):

- [ ] `setup-termux.sh` smoke test passes (`signal-cli --version` prints).
- [ ] Linking completes (QR or deep-link) and Signal shows the linked device.
- [ ] `pull-groups.sh` writes a sensible group count.
- [ ] `broadcast.py --limit 2` delivers to two test groups.
- [ ] A full run completes without the phone sleeping mid-way (screen on / charging).
- [ ] *(If scheduling)* a scheduled time fires with the screen off (check `logs/cron.log`).
- [ ] *(If scheduling)* it still fires after a reboot (Termux:Boot working).
