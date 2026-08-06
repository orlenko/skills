# Agent Observer 0.7 — Widening Observed Attention

Milestone addendum to `agent-observer-v0-spec.md`. This document records what
0.7.0 added, what it deliberately did not add, and which of the base spec's
claims it changes.

## Why this milestone exists

Through 0.6.4 exactly one signal could place a project in `Needs a look`: a
Claude `AskUserQuestion` tool call. Two consequences followed, and both
contradicted the product's own purpose.

Codex sessions could never produce observed attention at all, because Codex
rollout logs expose no structured decision request. Roughly half the operator's
fleet was represented in the ledger only as activity.

More seriously, the most expensive stall in practice left no trace anywhere. A
session blocked on a permission prompt writes nothing to its transcript. The
turn does not end, so a `Stop`-style notification never fires, and the terminal
tab looks identical to one doing useful work. `PRODUCT.md` promises the operator
will notice stalled work; the mechanism to notice it did not exist.

0.7.0 adds two independent sources of observed attention and one probe for the
operator's own automation. None of them requires worker cooperation, and none of
them changes the trust model: everything below is a fact with a timestamp, not
an inference by a model.

## 1. Silence that owes a completion — `no_completion_observed`

The base spec named this inference rule and never implemented it; the kind
appeared only in a supersession clause that nothing could trigger. It is now
live, and it needs no configuration, which makes it the default safety net for
both providers and for remote peers.

A session qualifies when it owes a completion — its last recorded event was a
user message, a turn start, or tool activity — and it has then gone quiet.

The claim is bounded on **both** sides, and the upper bound is the load-bearing
half:

- **Debounce (10 minutes).** Shorter silence is ordinary work.
- **Window (6 hours).** Longer silence is an abandoned session, which the
  `stale` view already describes. A claim that outlives the window is retired
  automatically rather than accumulating.

Without the upper bound, enrolling a machine with hundreds of historical
sessions would manufacture hundreds of attention items on first scan — the exact
failure the spec's D0 gate calls generating "more attention debt than it
recovers."

The wording is deliberately weaker than the temptation. The claim is "no
completion observed since your last message," never "blocked on a permission
prompt." A long compile produces the same evidence, and the operator can see
which it is in one glance. Naming the evidence rather than the cause is what
keeps this an observed fact.

Resumption clears it with no operator action: any meaningful event newer than
the claim supersedes it.

## 2. Direct harness notices — `input_requested`

Where the silence rule is a bounded inference, this is a direct ask, and it
arrives in seconds rather than after a ten-minute debounce.

Both harnesses run a local program at the moment they need the operator: Claude
through its `Notification` hook, Codex through `notify`. Those invocations
append one line to a machine-global spool, and the collector turns each into an
observed finding on its next pass.

Three properties matter:

- **A hook must never break its session.** Every writer path swallows its own
  errors and exits zero. `signal --strict` exists so setup can be verified
  deliberately, and is never what a hook itself runs.
- **The spool is untrusted even though it is local.** Every field is validated
  and bounded on read; a malformed line is skipped, not repaired.
- **A signal without a session id is dropped, never guessed.** Binding a notice
  to "probably the most recent session in that directory" would be inference
  wearing an observed badge. Codex reports approval requests only when its
  notice carries a session id and cwd.

Turn-completion notices are ignored on purpose. A finished turn is activity, and
promoting it would rebuild the lifecycle wall the ledger exists to avoid.

Supersession here is **time-aware**, unlike the original clause: only an event
newer than the request retires it. Collection lags the harness by a scan
interval, so a record that predates the request must not clear a question the
operator has not answered.

The two sources compose. If the hooks are never configured, the silence rule
still catches a blocked session within ten minutes. If a signal is lost — the
one real gap, a crash between draining the spool and the database write — the
silence rule recovers it on the same schedule.

Wiring is opt-in and printed by `agent-observer signal-hooks`. The Observer does
not edit anyone's settings files.

## 3. Sentinels — watching the operator's own automation

Scheduled jobs fail quietly. During this milestone's own research a dispatch
queue was found holding ten briefs, the oldest waiting twenty-nine days, with
the evidence sitting in a directory nobody reads.

A sentinel is an explicitly enrolled probe over evidence a job leaves on disk.
Two probes exist, both pure `stat` calls — no subprocess, no network, no model:

- `file_freshness` — a target must have changed within a threshold.
- `queue_backlog` — a directory's oldest item must be younger than a threshold.

**Retirement is first class, and this is the point of the design.** Each entry
carries an expiry. Past it, the check reports `expired` — "confirm this system
still exists or remove the check" — rather than `failing`. The motivating case
was real: a health probe for the retired `a2a` daemon had been printing
`NOT RUNNING` into every report for months. A watchdog that cannot be retired
becomes a permanent false alarm, and permanent false alarms are how an attention
surface loses the operator's trust. `sentinel-affirm` is the re-affirmation.

Sentinels are machine-scoped, so they surface as dashboard notices rather than
entering the project ledger. They are **not** included in remote snapshots in
0.7.0: the snapshot validator rejects unknown top-level fields, so adding them
would break any peer that has not upgraded. Carrying a peer's automation health
home is left to a later milestone that can version the snapshot schema.

Note the limitation plainly: these probes detect **silence in a chosen
artifact**, not failure. Choosing an artifact that only a successful run
updates is the operator's job. A log file that records failures stays fresh
while the job is broken.

## 4. Per-item dismissal

Dismissal was previously one lever per project: it marked every open finding
seen and suppressed the whole review. An operator who wanted to answer one
question and keep its two siblings visible had no way to say so.

Each attention item now carries the reference needed to dismiss only itself.
Observed findings dismiss by finding id — which finally gives the long-orphaned
`POST /api/findings/seen` handler a caller. Reviewed loose ends dismiss by
**content-addressed fingerprint**, generalizing the mechanism 0.5 already used
for remote projects.

The fingerprint is the reason this needs no disposition table. An item returns
exactly when its substance changes — a different assessment, a different cited
message — and never merely because a later review renumbered it. There is no
mutable per-item state to migrate, no `handled_elsewhere`/`still_relevant`
vocabulary, and no snooze. The project-level `Dismiss all current attention`
remains for the common case.

The project-level fingerprint is byte-identical to 0.6.4's, pinned by a test, so
remote dismissals recorded by an earlier version still apply after upgrade.

## Changes to base-spec claims

- **Section 5.4 — `child_activity` withdrawn.** Collection excludes subagent
  transcripts, so no parent session ever received attached child activity; the
  kind was an unreachable branch. Restoring it requires first deciding whether
  subagent traces are evidence at all, which section 19 still lists as open.
- **Section 16 — the analysis-disclosure contradiction is resolved.** The clause
  required analysis to be user-triggered with a *pre-launch* disclosure, while
  D0 as built is unattended and hourly, leaving the disclosure nowhere to live.
  The consent requirement stands; its placement moved. Consent is given once by
  attaching an analyzer; the analyzer panel now shows message count, byte bound,
  and provider per job after the fact; detaching stops all model calls. A
  genuinely user-triggered launch returns with D1's manual control.
- **`continuity` facet — the code is canonical.** The spec enumerated nine
  values and the UI handoff seven, while the implementation emits `current`,
  `stale`, and `not_analyzed`. Rather than build seven states nothing produces,
  treat the three as the contract and let D1 widen it if the states earn their
  keep.

## Deliberately not in this milestone

- **The autonomous wake-and-review overseer loop.** Still the largest named gap,
  and still demoted. It re-opens the token-burn failure mode that forced the
  dormant-analyzer design, and its marginal value shrinks now that real-time
  observed signals exist. The human's attention, not the model's, was the
  scarce resource.
- **Cross-machine attention delivery.** Local voice notification already works;
  the remote peer runs one to three sessions. Parked until that changes. The
  existing remote transport is untouched.
- **Deep-linking an attention item back to its terminal workspace.** Two clicks
  today, one after; the operator rates the discomfort tolerable.
- **Merging undrudge.** The two tools answer different questions on different
  clocks. Sentinels watch undrudge as one more piece of automation, which is the
  correct amount of coupling.
- **Rebuilding the full-projection round trip or the remote identity model.**
  Both are real ceilings; neither binds at twelve projects. The string-prefix
  remote namespacing is the first wall a host filter will hit.

## Acceptance

Twenty-two tests cover this milestone: `tests/test_stall.py`,
`tests/test_signals.py`, `tests/test_sentinels.py`, `tests/test_dismissal.py`.
They assert the behavior above, including the bounds that prevent attention
debt, the refusal to guess a session binding, expiry beating failure for an
unaffirmed check, and fingerprint stability across the refactor.

Scenarios 41–80 of the base spec remain written against unbuilt D1 behavior.
This milestone does not claim to close them; it narrows what D1 still owes to
group roll-ups, source-backed slice navigation, an append-only semantic ledger,
and a manual review launch.
