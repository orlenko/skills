# Orlenko Skills

An installable library of skills and plugins shared by Codex and Claude Code.

## Agent Observer

`agent-observer` is a passive local and explicitly enrolled remote-agent
dashboard described in
[`docs/agent-observer-v0-spec.md`](docs/agent-observer-v0-spec.md). The current
vertical slice provides:

- an explicit project watchlist;
- bounded Claude and Codex session discovery;
- canonical visible-message and turn-boundary parsing;
- item-correlated Claude `AskUserQuestion` findings;
- bounded silence detection: a session that owes a completion and has been quiet
  for ten minutes becomes observed attention, and the claim retires itself after
  six hours rather than becoming permanent backlog;
- opt-in harness signals from Claude's `Notification` hook and Codex `notify`,
  so a session blocked on a permission prompt surfaces in seconds — printed by
  `signal-hooks`, never installed on the operator's behalf;
- sentinels over the operator's own background automation, with file-freshness
  and queue-backlog probes and mandatory re-affirmation so a check for a retired
  system asks to be removed instead of alarming;
- durable SQLite byte checkpoints and partial-record recovery;
- source replacement/truncation generations;
- read-only Git branch sampling;
- collector health and a local CLI status surface;
- a private authenticated localhost dashboard;
- a recent-project enrollment combobox with bounded session topic cards;
- per-item and project-level attention dismissal, where reviewed loose ends are
  dismissed by content fingerprint and return only if their substance changes;
- live Activity or static Project sorting;
- managed collection and server sidecars;
- a dormant subscription-backed Claude or Codex analyzer sidecar selected by
  the session that invokes the skill;
- deterministic activity-volume gating, a ten-minute quiet debounce, hourly
  model batches, bounded review packets, deterministic evidence-block
  references, and exact evidence validation before dashboard publication;
- crash-safe accepted cutoffs and takeover by a replacement Observer session;
- symmetric pinned-TLS LAN/Tailscale snapshot transport: either the dashboard
  listens for pushes or the watched peer listens for dashboard pulls, using
  single-use `ao1.` enrollment and revocable durable credentials;
- remote collectors and dormant subscription analyzers with no remote web UI;
- bounded node-scoped snapshot projection, uploader-epoch fencing, and
  host-labeled local/remote projects in one dashboard.

The current **D0 model review** selects one changed worker session and at most
40 visible messages per packet, returns at most three suggestions, and runs the
selected provider CLI with subscription authentication and no persistent
provider session. Deterministic code waits for at least two substantial model
messages, two user messages, 1,200 model characters, and ten minutes of quiet.
It then drains eligible sessions in one batch and will not invoke a model-backed
batch again for an hour. With no qualifying activity it makes no model call and
uses no tokens. The invoking Observer session is persistently excluded from
collection when its provider exposes the session ID; its workspace cannot be
added to the watchlist.

The 0.7 milestone that widened observed attention is described in
[`docs/agent-observer-0.7-attention-sources.md`](docs/agent-observer-0.7-attention-sources.md),
including what it deliberately left unbuilt.

Sentinels are machine-scoped and are not carried in remote snapshots, because
the snapshot validator rejects unknown fields and would break a peer that has
not upgraded. Filesystem notifications, deep remote conversation replay, and the
full productized semantic ledger remain follow-up work. Remote transport deliberately
supports only addresses directly reachable over a LAN or Tailscale; it contains
no relay or NAT-traversal service. Explicit `add` and `rescan` operations may
take several seconds with thousands of provider files; steady-state scans remain
proportional to watched sources.

### Install and invoke

Use the same marketplace already used for Agent Pair.

Claude Code:

```sh
claude plugin marketplace add orlenko/skills
claude plugin install agent-observer@orlenko-skills
```

Codex:

```sh
codex plugin marketplace add orlenko/skills
codex plugin add agent-observer@orlenko-skills
```

Start a fresh session, enter a project, and invoke:

```text
Codex: $agent-observer:observe
Claude: /agent-observer:observe
```

Run the command from a fresh, dedicated Observer workspace such as
`~/personal/dash`. It starts the collector, dashboard, and dormant analyzer
without watching that workspace, then returns immediately. It also returns a
single-use `ao1.` remote enrollment key. Use the dashboard or `add PATH` to watch projects; use `status`,
`rescan PATH`, or `stop` for narrower operations.

On an Ubuntu machine reachable over the same LAN or Tailscale network, install
the plugin, start a dedicated workspace such as `~/personal/dash`, and invoke:

```text
Codex: $agent-observer:remote ao1.KEY_FROM_HOME
Claude: /agent-observer:remote ao1.KEY_FROM_HOME
```

That machine starts deterministic collection and its dormant
subscription-backed analyzer, uploads bounded snapshots to the home dashboard,
and starts no web UI. Add or remove its watched projects from that remote
Observer session. To advertise a specific Tailscale or LAN address from the
home session, invoke `enable-remote ADDRESS` once; future `observe` invocations
reuse it and issue a fresh key.

If the dashboard host cannot accept inbound TLS, reverse only the connection
direction. On the watched peer run:

```text
Codex: $agent-observer:remote listen
Claude: /agent-observer:remote listen
```

It returns a single-use `ao1.` key for its dedicated snapshot listener. On the
chosen dashboard host run `observe connect ao1.KEY_FROM_PEER`; that dashboard
then pulls the same bounded projections. Collection and analysis remain on the
watched peer in both modes. Either physical machine may host the dashboard;
whichever side can accept direct LAN or Tailscale TLS can be the listener.

Run directly from a checkout:

```sh
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash --json supervisor-begin --provider codex --allow-cross-provider
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash --json remote-enable --advertise 100.64.0.10
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash --json remote-listen --provider codex --allow-cross-provider --advertise 100.64.0.10
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash --json remote-connect ao1.KEY_FROM_PEER --provider codex --allow-cross-provider
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash supervisor-status
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash add ~/code/ops2
plugins/agent-observer/bin/agent-observer --workspace ~/personal/dash supervisor-stop
```

Skill-driven state lives at `<observer-workspace>/.agent-observer/`. Direct CLI
commands without `--workspace` retain the legacy default
`~/.local/state/agent-observer`. Use
`AGENT_OBSERVER_HOME`, `AGENT_OBSERVER_CLAUDE_ROOT`,
`AGENT_OBSERVER_CODEX_ROOT`, `AGENT_OBSERVER_CODEX_ARCHIVE_ROOT`, or
`AGENT_OBSERVER_CODEX_SESSION_INDEX` to isolate development and tests. Codex
session names are read from its local `session_index.jsonl`; Claude names come
from provider session metadata. The state directory is mode `0700`; the SQLite
database is mode `0600`.

For an editable installation:

```sh
python3 -m pip install -e plugins/agent-observer
agent-observer --help
```

The UI design contract for specialized design tools is
[`docs/agent-observer-ui-handoff.md`](docs/agent-observer-ui-handoff.md).

## Agent Pair

`agent-pair` connects exactly two agent sessions through a direct, durable text
mailbox. It works on one machine or between machines that can reach the host on
the network. The transport uses a temporary self-signed TLS certificate pinned
in a single-use `ap1.` invite; no central relay or account is required.

Current scope:

- Two peers and text messages only.
- Durable local inbox/outbox and idempotent network delivery.
- Delivery states: queued, delivered to the peer monitor, and handled.
- Local-first `finish` and `close`. Both complete without the peer, and the
  monitor delivers the deferred notice once the peer is reachable again.
- Automatic monitor startup on every pair command, with a process-lifetime
  lock preventing duplicate monitors after a delayed heartbeat.
- A 24-hour default pair lifetime, configurable from 5 minutes to 7 days.
- Claude Code idle-session reawakening through `asyncRewake`.
- Codex idle-session wake-up through native `codex queue`, with lifecycle
  reminders as a fallback (verified with Codex 0.154.0).

Python 3.10+ and `openssl` are required.

### Install from GitHub

Claude Code:

```sh
claude plugin marketplace add orlenko/skills
claude plugin install agent-pair@orlenko-skills
```

Codex:

```sh
codex plugin marketplace add orlenko/skills
codex plugin add agent-pair@orlenko-skills
```

Start a new session after installation so skills and hooks are loaded. Codex
will ask you to review and trust the plugin hooks before they can run.

### Pair two sessions

On the session that will listen:

```text
Codex: $agent-pair:pair
Claude: /agent-pair:pair
```

Give the returned `ap1.` invite to the other session. On that session:

```text
Codex: $agent-pair:pair ap1....
Claude: /agent-pair:pair ap1....
```

Hosting binds to all IPv4 interfaces by default and advertises detected local
addresses. If detection chooses the wrong interface, the agent can use:

```sh
agent-pair host --provider codex --advertise 192.168.1.20
```

Do not post an unexpired invite publicly. It contains a single-use join secret.

### Commands

The installed skill routes these natural commands:

```text
$agent-pair:pair send I finished the parser; please review src/parser.py
$agent-pair:pair inbox
$agent-pair:pair status
$agent-pair:pair close
```

Use `/agent-pair:pair ...` in Claude Code.

For direct debugging, the bundled CLI supports:

```sh
plugins/agent-pair/bin/agent-pair --help
```

Runtime state defaults to `~/.local/state/agent-pair` or
`$XDG_STATE_HOME/agent-pair`. Set `AGENT_PAIR_HOME` to isolate tests. State
files are private to the current OS user.

### Wake behavior

The monitor continuously moves remote messages into a durable local inbox,
acknowledges only after the local write succeeds, retries a local outbox, and
sends presence heartbeats.

Claude Code starts a background `Stop` hook for each participating session.
When mail arrives, that hook exits with code 2 and `asyncRewake` asks Claude to
process the inbox even while idle. The watcher is deduplicated per session.

Codex monitors use the native queue API when unhandled mail arrives.
Host/accept and the owning session's hooks bind the
exact thread UUID and `CODEX_HOME`. Loaded idle threads start a turn without a
user prompt; busy threads process the notice after the current turn. Exited
threads keep the notice until resumed. Native cross-process queue discovery
may take about ten seconds. This path was verified with Codex 0.154.0.

The notice points at the current inbox; it contains no peer text. Successful
submissions are deduplicated across monitor restarts. Both plugins share a
60-second wake limit per Codex thread; arrivals during the cooldown coalesce
and the inbox is rechecked before submission. Only one notice may wait behind
a busy turn. Claiming, finishing, and Stop-hook previews cancel redundant
notices before the current turn ends; empty inboxes cancel stale notices,
including those queued by older versions. Failed submissions retry within
the same rate limit and appear in `status --json` under `wake.last_error`;
queue acceptance never
marks peer mail handled. `wake.state=armed` reports a registered target, not
proof that the thread is loaded. Older Codex installations still get lifecycle
reminders and can use explicit `inbox` or `wait`. Set `AGENT_PAIR_CODEX_BIN`
to the real executable before binding when a custom wrapper redirects homes.
AIQ is bypassed for queue operations so the registered home is preserved.
After upgrading either plugin, run `monitor --restart --provider codex` with
the retained `--endpoint-id` or `--member-id` once to load the new code; newly
created pairs and memberships start the current monitor. A process-lifetime
lock permits only one monitor per mailbox, even when a slow request makes its
heartbeat stale.

Stop hooks peek without claiming and inject each waiting message's sender,
claim token, and up to 4 KiB of body text. A truncated preview points at the
full local row. Agents act on that untrusted peer input, then use the nudge's
direct `finish` command to mark only processed messages handled; an interrupt
before that command leaves the mail waiting.

## Agent Orchestra

`agent-orchestra` carries durable text mail between any number of agent sessions
on any number of machines. One hub process holds every mailbox, the member tree,
and a task view derived from the messages themselves. Members connect outbound
to the hub over TLS pinned by a single-use `or1.` invite, so only the hub has to
be reachable. No relay and no account are involved.

Three roles:

- **Hub.** One detached process, one SQLite store. It never reads bodies and
  never routes on content. It runs on the always-on machine and is never the
  conductor. `hub start` creates no membership, so the session that starts a hub
  can neither send nor receive.
- **Conductor.** One member at a time, on the machine the human drives. It owns
  the `conductor` alias, mints invites of any role, reassigns the role, kicks
  members, and closes the orchestra.
- **Player.** Every other member. A player mints invites for its own children,
  which gives the tree behind the `parent`, `children`, and `siblings` aliases.

Python 3.10+ and `openssl` are required.

### Install from GitHub

```sh
claude plugin marketplace add orlenko/skills
claude plugin install agent-orchestra@orlenko-skills

codex plugin marketplace add orlenko/skills
codex plugin add agent-orchestra@orlenko-skills
```

Start a new session after installation so skills and hooks are loaded. Codex
will ask you to review and trust the plugin hooks before they can run.

### Bring one up

1. On the always-on machine, run `/agent-orchestra:orchestra hub` and copy the
   printed `or1.` conductor invite.
2. On the conductor machine, run `/agent-orchestra:orchestra or1....` with that
   invite.
3. From the conductor, run `/agent-orchestra:orchestra invite` once per player
   and hand each string to its machine.
4. Each player redeems its own invite the same way the conductor did, and mints
   child invites with the same command.

Use `$agent-orchestra:orchestra ...` in Codex. Do not post an unexpired invite
publicly; it carries a single-use join secret.

### Commands

```text
$agent-orchestra:orchestra send ACT assign ...
$agent-orchestra:orchestra inbox
$agent-orchestra:orchestra status
$agent-orchestra:orchestra members
$agent-orchestra:orchestra tasks
$agent-orchestra:orchestra leave
```

The bundled CLI adds `hub start|ensure|status|list|unit|invite|conductor|kick|close`
plus `join`, `wait`, `finish`, `events`, `message`, and `close`:

```sh
plugins/agent-orchestra/bin/agent-orchestra --help
```

Runtime state defaults to `~/.local/state/agent-orchestra` or
`$XDG_STATE_HOME/agent-orchestra`. Set `AGENT_ORCHESTRA_HOME` to isolate tests.
State files are private to the current OS user.

### Wake behavior

Each member runs a monitor that long-polls the hub, writes every message into a
durable local inbox before acknowledging it, replays a local outbox, and sends
presence heartbeats. Claude Code starts a background `Stop` hook in the session
that ran `join`, and when mail arrives that hook exits with code 2 so
`asyncRewake` can process the inbox while the session is idle. Codex uses the
same native queue wake path described for Agent Pair above, binding its exact
thread and account home on `join` or its owning lifecycle hook. Only member
mail wakes a session; system presence events and task attention do not. The
executable override is `AGENT_ORCHESTRA_CODEX_BIN`. Lifecycle reminders remain
a fallback, and `status --json` reports queue failures in `wake.last_error`.

### Jev checks

Off by default. With `AGENT_ORCHESTRA_JEV=1` and `TYPESAFE_API_KEY` exported
where the session starts, agent-orchestra calls TypeSafe Jev in two places.
Both send content to that third party.

- `send`: when `NEED` is `none` and Jev gives p >= 0.9 that the body asks for
  a reply, the result carries a warning. Delivery is unaffected. On 1,221 real
  messages this was the only one of five checks that beat the sender's own
  headers (7 of 8 labelled warnings correct).
- The monitor logs Jev's read of each seated session's transcript tail next to
  the task timers (`<member>.jev.jsonl` in the runtime dir), for a later
  comparison. Nothing reads it.

`AGENT_ORCHESTRA_JEV_SHADOW=1`, the 0.2.3 name, still works.

## Agent Nudge

Coding agents often stop short of their goal. They report and stop, or they
say they will act when a CI job finishes and set nothing up to wake them. A
single outside line like "still waiting?" usually gets them going again.
`agent-nudge` is a per-machine daemon that sends that line to Claude Code and
Codex sessions running in tmux.

Every 30 s it reads each agent pane with `tmux capture-pane`. It types one
question into a pane only when all of these hold:

- the screen above the input box has not changed for 10 minutes;
- the input box is empty (dim suggestions count as empty);
- no one has typed in that tmux session for 5 minutes;
- TypeSafe Jev reads the screen as idle, with no question for the user.

It asks whether the goal is done or blocked. If the agent said it was waiting
on something and nothing in the footer is watching, it says that instead. Jev
also decides directly whether asking again could plausibly help. The daemon
gives it trusted continuity facts that a clipped terminal screen cannot prove:
the unchanged duration, consecutive nudge/reply count and span, and whether a
non-nudge wake interrupted the chain. An unchanged pane that explicitly
answered "the goal is done" therefore stays quiet instead of receiving the same
question at ever-longer intervals. Authoritative new Orchestra mail or an open
task still overrides that judgment. When a repeat is useful, its wait triples. If the agent answered a nudge by
naming an obstacle and stopping ("blocked on…", "waiting for #73 to merge",
"asked for a ruling first"), the next nudge pushes back once: check whether
someone already fixed it, ask your conductor or partner, or fix it yourself
in a stacked PR and carry on.
It starts in dry-run and logs what it would have typed.

```sh
claude plugin install agent-nudge@orlenko-skills     # or: codex plugin add agent-nudge@orlenko-skills
echo 'TYPESAFE_API_KEY=...' > ~/.config/agent-nudge/env && chmod 600 ~/.config/agent-nudge/env
plugins/agent-nudge/bin/agent-nudge install          # launchd or systemd --user
plugins/agent-nudge/bin/agent-nudge status
plugins/agent-nudge/bin/agent-nudge log
plugins/agent-nudge/bin/agent-nudge mode live        # after a dry-run day looks right
```

Opt a pane out with `tmux set-option -p -t <pane> @nudge off`. With the key
set, an idle agent's last ~40 screen lines go to TypeSafe, a third party, once
per stop.

## Undrudge Workflows

`undrudge-apply` is the acting half of [`undrudge`](https://github.com/orlenko/undrudge),
a background watchman that mines shell history and agent transcripts for
automation opportunities. Recommendations accumulate because acting on one
normally means abandoning whatever the session is doing, and `undrudge dispatch`
pushes work into a repository clone that may already be occupied or dirty.

`undrudge here` inverts that: it reports which open recommendations belong to
the repository a session is already standing in. This plugin is the part that
acts on one of them.

Each invocation disposes of exactly one recommendation — implemented behind a
draft pull request, dismissed with a reason, or handed back as needing a human —
and flips its status in the same session so the pile actually shrinks. It
refuses to implement anything when the working tree is dirty, but still names
the recommendation it would have taken, because losing that observation is the
problem it exists to solve.

It requires the `undrudge` CLI on `PATH` and a git repository. Cross-cutting and
agent-global recommendations are out of scope for `undrudge-apply`.

The same plugin also carries `undrudge-triage`, a cross-agent conversation for
the global backlog. It loads every logged finding as JSON, selects the most
valuable-looking one, reads and validates it against current reality, then asks
the user to implement, dismiss, hand off, or defer. It recognizes duplicate
daily and weekly findings and recommends retaining one canonical copy.

Triage never implements work or opens pull requests. It changes status only
after explicit approval: dismissals become `dismissed`, confirmed external
handoffs become `dispatched`, and defer leaves the finding untouched. Repo-local
implementation stays in `undrudge-apply`.

Triage requires an `undrudge` release whose `list` command supports `--json`;
it fails closed without changing status when that capability is unavailable.

### Install and invoke

Claude Code:

```sh
claude plugin marketplace add orlenko/skills
claude plugin install undrudge-apply@orlenko-skills
```

Codex:

```sh
codex plugin marketplace add orlenko/skills
codex plugin add undrudge-apply@orlenko-skills
```

From a session inside the target repository:

```text
Codex: $undrudge-apply:undrudge-apply
Claude: /undrudge-apply:undrudge-apply
```

Pass a rec id to work that recommendation instead of the best match, and re-run
to take the next one.

From any session, review the global backlog one finding at a time:

```text
Codex: $undrudge-apply:undrudge-triage
Claude: /undrudge-apply:undrudge-triage
```

## Repository layout

```text
.agents/plugins/marketplace.json      Codex marketplace
.claude-plugin/marketplace.json       Claude Code marketplace
plugins/agent-pair/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/pair/SKILL.md
  bin/agent-pair
plugins/agent-nudge/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/nudge/SKILL.md
  bin/agent-nudge
plugins/agent-observer/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/observe/SKILL.md
  bin/agent-observer
plugins/design-skeptic/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/design-skeptic/SKILL.md
  agents/design-skeptic.md
plugins/undrudge-apply/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/undrudge-apply/SKILL.md
  skills/undrudge-triage/SKILL.md
```

## Development

```sh
python3 -m unittest discover -s plugins/agent-observer/tests -v
python3 -m unittest discover -s plugins/agent-pair/tests -v
python3 -m unittest discover -s plugins/agent-nudge/tests -v
# Optional: real Codex queue regression with a local mock model (no account needed).
AGENT_TEST_CODEX_BIN=/path/to/codex python3 -m unittest discover \
  -s plugins/agent-pair/tests -p test_codex_native_wake.py -v
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/agent-pair/skills/pair
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/agent-observer/skills/observe
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/undrudge-apply/skills/undrudge-apply
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/undrudge-apply/skills/undrudge-triage
python3 path/to/plugin-creator/scripts/validate_plugin.py \
  plugins/agent-pair
python3 path/to/plugin-creator/scripts/validate_plugin.py \
  plugins/agent-observer
claude plugin validate plugins/agent-pair
claude plugin validate plugins/agent-observer
claude plugin validate plugins/undrudge-apply
```

The validator script locations depend on the local Codex installation; replace
the placeholders with the corresponding built-in skill paths.
