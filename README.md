# Orlenko Skills

An installable library of skills and plugins shared by Codex and Claude Code.

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

## Repository layout

```text
.agents/plugins/marketplace.json      Codex marketplace
.claude-plugin/marketplace.json       Claude Code marketplace
plugins/agent-pair/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/pair/SKILL.md
  bin/agent-pair
```

## Development

```sh
python3 -m unittest discover -s plugins/agent-pair/tests -v
python3 path/to/skill-creator/scripts/quick_validate.py \
  plugins/agent-pair/skills/pair
python3 path/to/plugin-creator/scripts/validate_plugin.py \
  plugins/agent-pair
claude plugin validate plugins/agent-pair
```

The validator script locations depend on the local Codex installation; replace
the placeholders with the corresponding built-in skill paths.
