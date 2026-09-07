# Send throttling recovery

## Problem and approved behavior

The supplied incident log contained 628 `RetryLaterException: Retry after 4 seconds`
errors affecting 145 distinct groups, two lost-response errors, and three membership
errors for one group. These are attempts, not counts of definitively undelivered
messages. No identifiers, attachment paths, message content or raw incident log are
retained in this document or the regression fixtures.

The old classifier labelled every retry-later error `throttled=False`, used two
five-second retry waits, and then failed the group. The wrapper
`AttachmentInvalidException` does not establish a corrupt or oversized photo.
The upload service's reason for throttling, and any role of the VPN, remain unproven.

Approved requirements:

- Recognise `RetryLaterException` before generic attachment errors and honour the
  numeric retry-after delay. Membership failures are permanent and excluded from
  automatic retry and resume. Requests that may have delivered remain unconfirmed.
- One recovery coordinator per broadcast covers initial dispatches and retries in
  both sequential and parallel operation. Use monotonic time and interruptible waits.
- Pause admissions on throttling. Wait for admitted attempts to settle, then admit
  one recovery probe. Backoff is 30, 60, 120, 240, then 300 seconds, or a larger
  server delay. A successful probe restores configured concurrency with normal pacing.
- Automatically wait up to 15 minutes without a confirmed successful send after
  throttling begins. In-flight successes reset that window but do not bypass the
  server cooldown. If it expires, admit no more attempts and pause for manual resume.
  Already-admitted requests settle within the existing send timeout; they are not replayed.
- Preserve the complete ledger through pause, restart and repeated resume. Never
  replay successful, permanent-failure, skipped or unconfirmed groups. Only pending
  work and definite retryable failures are eligible. Validate the saved draft/style.
- Scheduled runs record a pause and cannot bypass unresolved saved work. Stop,
  logout and erase remain responsive. Do not spawn competing Signal profile owners.
- Show the sanitised cause, countdown, probe, recovery and final pause in Recent
  activity. Persist transitions, not every countdown tick. Diagnostic write failures
  must not change delivery outcomes. Never log provider text, identifiers, file paths,
  phone numbers or message content.

## Interfaces and persistence

`engine.broadcast(..., resume=True)` reuses the existing ledger and original run ID.
A normal return remains a list of `GroupSendResult`. A deliberate throttle pause
raises `BroadcastPaused` carrying partial results across the original group set.
`waiting` results are pending, not failures; `permanent` failures require membership
repair. `retryable` excludes both alongside successful, skipped and unconfirmed results.

The existing JSON checkpoint format gains optional run ID, message style and paused
metadata plus `waiting` and `permanent` group states. Older checkpoints remain readable.
Its opaque group IDs are private recovery state, not diagnostic log fields. A killed
`attempting` request becomes `uncertain` on resume and remains excluded across resumes.

The Mac worker saves partial counts without arming the broadcast cooldown on pause,
and sends a distinct paused event. The service retains recovery status for refreshed
or reopened windows, computes the remaining wait, and records scheduled pauses.
The Mac UI shows the reason and a Resume remaining action; uncertain-only runs cannot
be resumed. A paused run must be explicitly resumed or discarded.

`logs/send-diagnostics.jsonl` remains always on and bounded, with one rotated file.
Attempt records retain timing, original group position and a fixed error category.
Transition records add `event` (`throttled`, `retrying`, `recovered`, `paused`) and
numeric `retry_after` seconds. Countdown rendering does not append new records.

## Validation and performance boundaries

Tests use the sanitised error shapes in `tests/test_throttle_recovery.py`, fake
clocks, disposable storage and fake Signal transports. Coverage includes concurrent
admission, single probes, long server delays, success resetting the recovery window,
late success after expiry, Stop, repeated resume and changed drafts/styles. Mac
worker/service/UI checks cover partial summaries, scheduled pauses and activity.
No live customer messages are sent.

A synthetic 300-group run with a 132,000-character message and 12 attachment paths
is checked at concurrency 1 and 5: exactly one successful dispatch per group. This
measures application bookkeeping, not Signal's network fan-out or upload cost.

Local checkpoint-only measurements on 2026-09-07, using atomic writes to disposable
storage and two state writes per group:

| Groups | Checkpoint writes | Serialized bytes | Elapsed seconds |
| --- | ---: | ---: | ---: |
| 100 | 201 | 587,516 | 0.059 |
| 500 | 1,001 | 14,137,116 | 0.509 |
| 1,000 | 2,001 | 56,274,116 | 1.323 |

Full-ledger rewrites have quadratic aggregate disk work. At these tested sizes the
measured bookkeeping time is far below the incident's roughly 48-minute error window.
This does not establish performance on the affected Mac or for larger accounts.
Storage-format changes, attachment upload caching and Signal protocol changes are
excluded from this repair. No unverified claim is made about quadratic network work.

## Review and rollout

Review baseline: `ebbe31d439b1e1054f8ee7a7ada432f59cf64e8e`.
After initial implementation and tests, run fresh `/code-review` and `/simplify`
agent sessions in parallel, findings only. Both receive this specification, the
baseline-to-head diff and sanitised fixtures. The main agent validates every finding
and commits each accepted review fix separately, then reruns affected checks and the
full suite. Preserve unrelated work.

Both fresh reviews completed against `f3d507d`. They independently reproduced one
shared finding: the portable CLI cleared an uncertain-only checkpoint after resume,
allowing a later scheduled invocation to resend it. Accepted as a duplicate-delivery
defect and fixed separately. The regression completes repeated pause/resume, then
checks repeated unattended calls, a changed draft and an empty explicit resume:
all retain the checkpoint and dispatch nothing. No other actionable findings.
Native validation passed two UI tests and five integration tests; one optional
integration test was skipped. All transport calls were synthetic.

Finish or deliberately stop the current real broadcast before updating/restarting
the app/service. New jobs use the updated engine. A real large-group canary is a
separate authorisation step; compare sanitised timings, throttling, recovery and
final outcomes. Unit and native fixture tests cannot guarantee Signal availability
or reconstruct missing delivery acknowledgements from earlier runs.
