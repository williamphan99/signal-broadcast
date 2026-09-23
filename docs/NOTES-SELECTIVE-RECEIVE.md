# Selective Note-to-Self receiving

Setup builds a small receive-only Java overlay against the pinned signal-cli 0.14.7
JVM distribution. It identifies self-addressed sync messages before downloading
attachments. Other conversations still update Signal protocol/group state, but their
media, previews, quote thumbnails and attachment-backed long text are not fetched.
All ordinary attachments on Note-to-Self messages remain enabled. View-once and
expiry handling in the app remain unchanged.

The original `bin/signal-cli` and its libraries are not modified. Only Notes and group
refresh select `bin/signal-cli-notes`. Sends, linking and other commands use the stock
launcher. This does not introduce another Signal process sharing the account: the
existing operation lock still serialises work.

`notes_receiver.py` verifies the upstream source SHA-256, applies the checked-in patch
without fuzz and compiles it with Java 25. A manifest binds the overlay, original
launcher, modified launcher, upstream library, patch and source version together.
A stale, missing or altered component falls back to full attachment downloads and
reports compatibility mode. The manifest is published last. Installation retains
both original and modified Java source beside the overlay.

Update detection includes the installer and patch directory, so **Update → Finish
update** runs Setup. Setup builds the receiver before stopping the existing Mac
service. Build failure is reported; it never installs a partially verified filter.
A successful Notes check records:

```
receive mode: media=note-to-self receiver=selective-0.14.7
```

This is selective attachment downloading, not a per-chat server query. Backlogs,
Signal errors, group-state fetching and the note's own media can still delay arrival.

## Verification

The opt-in Java integration test executes the actual upstream receive handler against
its release libraries with a local-only attachment helper. It runs the same fixtures
against stock and patched classes. It does not use a Signal account, contact the
Signal service or measure real-network speed.

- Stock handler: 202 unrelated media-download calls. Selective handler: zero.
- Albums of 1, 10, 16 and 30 mixed image/video attachments; 3 MiB per file.
- Every selected file compared byte-for-byte, up to 90 MiB per album.
- 516,000-character multilingual, multiline attachment-backed text retained exactly.
- Text-only notes, missing destinations, outgoing DMs, incoming DMs, modern/legacy groups and canonical self identity aliases.
- Unrelated long-text fallback cannot start an attachment download.
- Python integration covers large mixed-media notes, duplicate delivery, missing
  media, interrupted receives, fallback mode, verified launcher selection and JVM flags.
- Native Mac UI fixture passed: 16 photos and a long caption transferred from Notes
  into the composer intact. This uses a disposable UI, not a live Signal account.

To rerun with a disposable extracted upstream 0.14.7 distribution and its original
`IncomingMessageHandler.java` (checksum verified by the installer):

```sh
SB_SIGNAL_CLI_TEST_DIST=/path/to/disposable/signal-cli-0.14.7 \
SB_SIGNAL_CLI_TEST_SOURCE=/path/to/IncomingMessageHandler.java \
.venv/bin/python -m unittest discover -s tests -p test_notes_receiver.py
.venv/bin/python -m unittest discover -s tests
SB_RUN_MAC_UI=1 .venv/bin/python -m unittest discover -s tests -p test_mac_ui.py -k photo_drag
```

The customer's phone-to-Mac delivery is still a required device retest. After updating,
send a fresh captioned album to Note to Self, run the check, confirm every attachment
and full text, then use it as the draft. Collect `notes-debug.txt` from the new
`receive mode` line through completion if anything is missing or slow.

## Upstream and licence

The patch modifies AsamK/signal-cli's `IncomingMessageHandler.java`, version 0.14.7:
https://github.com/AsamK/signal-cli/blob/v0.14.7/lib/src/main/java/org/asamk/signal/manager/helper/IncomingMessageHandler.java

The upstream source and derived overlay are GPL-3.0; the upstream licence is included
in `scripts/signal-cli-notes/COPYING` and copied alongside the locally built overlay.
The modified source is retained there for inspection and rebuilding. This component
is separate from the repository's MIT-licensed Python application. Dependency upgrades
require reviewing/rebasing the patch, updating the source pin and rerunning the actual
handler integration tests; unsupported versions use the compatibility receiver.
