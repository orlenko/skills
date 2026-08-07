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
- Automatic monitor startup and restart on every pair command.
- A 24-hour default pair lifetime, configurable from 5 minutes to 7 days.
- Claude Code idle-session reawakening through `asyncRewake`.
- Codex lifecycle reminders plus best-effort desktop notifications. Codex does
  not currently support true asynchronous hook wakeups.

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

Codex currently parses but does not run asynchronous hooks. Its monitor still
runs continuously, shows a desktop notification when supported, injects
waiting-mail metadata on session start or the next prompt, and keeps a turn
open when mail is already waiting at `Stop`.

Peer message bodies are never injected by hooks. Agents explicitly claim them
from the local inbox and must treat them as untrusted peer input.

## Undrudge Apply

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
agent-global recommendations are out of scope; those are triaged by hand through
`undrudge browse` and `undrudge copy`.

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

## Repository layout

```text
.agents/plugins/marketplace.json      Codex marketplace
.claude-plugin/marketplace.json       Claude Code marketplace
plugins/agent-pair/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/pair/SKILL.md
  bin/agent-pair
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
```

## Development

```sh
python3 -m unittest discover -s plugins/agent-observer/tests -v
python3 -m unittest discover -s plugins/agent-pair/tests -v
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/agent-pair/skills/pair
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/agent-observer/skills/observe
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/undrudge-apply/skills/undrudge-apply
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
