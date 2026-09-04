# Agent Orchestra plan

Status: agreed plan, 2026-09-04. Nothing here is built yet.

## Goal

One durable messaging fabric for N agent sessions on N machines, with a
hierarchy on top: a conductor session that a human drives, player sessions on
other machines that each run their own teams of sub-agents, and optional child
members that a player spawns. Any member can message any member. The fabric
survives any single member going away, including the conductor. Messages are
written for agents, validated by the CLI, and summarizable without reading them.

Non-goals for the first release: internet relay or NAT traversal, automatic
conductor election, file transfer, remote shell, a work-item tracker with its
own API, a dashboard. Beads stays declined (see the 2026-08-06 memory); the task
view below is derived from messages and has no store of its own.

## What Agent Pair taught us

Each item below is a shipped fix or a design rule from `plugins/agent-pair`, and
each one becomes a day-one requirement here.

| Lesson | Where it came from | Orchestra rule |
| --- | --- | --- |
| Handling is a local fact; never let an unreachable peer block `finish` or `close`. | 622586d | `finish`, `leave`, `close` write locally first and sync best-effort with bounded retry. |
| Presence comes from the peer's heartbeat, never from reaching a server. | 622586d | Hub tracks `last_seen_at` per member; `status` reports `connected` / `stale N s` / `gone`. |
| Hooks bound to a cwd fire in every session sharing that cwd; `claude -p` children parked for hours and killed six fleet runs. | a4b483c | Hooks bind to the session that ran `join`; every other session is inert; `AGENT_ORCHESTRA_NO_WAIT=1` disables all hooks. |
| Stop-hook nudges must carry bodies, a claim token, and the exact finish command. | 49fcb4f | Same nudge shape, plus a total cap and NEED-first ordering (below). |
| A message read only by an agent needs a header grammar, falsifiable anchors, and a silence rule. | 84b1415 | Same ACT/RE/NEED/REF grammar, extended with TASK, validated by the CLI at send time. |
| `getfqdn()` reverse DNS stalls server bind on macOS. | 34234cf | Carried over verbatim. |
| Advisory claims over chat are a lock without atomicity; ownership belongs in shared state. | 84b1415 | The hub is shared state. `assign` with a TASK id is the one atomic claim, and the hub arbitrates it. |
| The plugin version is the cache key; bump three manifests. | memory | Release checklist item. |

Two more lessons come from Agent Observer's remote transport: identity is an
opaque node id plus a durable revocable credential, never a hostname, and a
single-use bootstrap invite (`ao1.`, `ap1.`) is the right way to hand out that
credential.

## Shape

Three roles. A machine can hold any combination.

- **Hub.** One detached process, one SQLite store. It holds every member's
  mailbox, presence, the member tree, and the derived task view. It is a dumb
  durable relay: it never reads bodies, never routes on content. Every member
  connects *outbound* to it over pinned TLS. Only the hub needs to be
  reachable. Put it on the always-on box (`ubuntu`, 100.93.107.44 on
  Tailscale). Hosting it on the laptop works and degrades exactly like Agent
  Pair does today.
- **Conductor.** A role held by exactly one member at a time. It gets the
  `conductor` alias, mints invites by default, and is where the human sits.
  Nothing else is special about it: it is a member with a mailbox.
- **Player.** Every other member. A player may mint invites for its own
  children, which gives the tree.

The tree is metadata: each member has a `parent` (null for the conductor).
It exists for addressing aliases and for status roll-up, nothing else.

### Why not the native Claude Code teammate channel

Claude Code can already message its own teammates on one machine and, through
Remote Control, the account's other sessions. That channel is Anthropic-mediated,
Claude-only (the Ubuntu box runs Codex too), has no offline queue, no hooks on
the Codex side, and no hierarchy. A player's lead session uses the native
channel for its sub-agents and the orchestra for everything that leaves the
machine.

### Partition behavior

| Event | What happens | What the agents see |
| --- | --- | --- |
| Conductor laptop sleeps | Hub keeps its mailbox. Players keep messaging each other and keep sending to `conductor`; those rows queue at the hub. | Players: `conductor stale 340s` in status, one `sys presence` event. No change to their work. |
| Conductor wakes | Its monitor reconnects, drains the queue in `sent_at` order, heartbeats. Hub emits `sys presence conductor connected absent_since=T` to everyone. | Players: send one `ACT status` if their last message to the conductor predates `absent_since`. The CLI computes that from the local `sent/` bucket and says `status_owed: true` in the nudge. |
| A player goes away | Same as above with the player's alias. Its children keep their own connections. | Conductor sees `stale`, then `gone` after the configured window. |
| Hub host goes down | Every member's outbox queues locally; monitors back off to 15 s and reconnect. | `send` returns `queued-locally`. `status` says `hub unreachable`. No message is lost. |
| Hub process dies, host up | Any orchestra command run on the hub host restarts it from the SQLite store. A printed (never installed) launchd/systemd unit is offered for reboot survival. | Same as above until restart. |

No election. If the conductor is gone for good, the human runs
`orchestra conductor MEMBER_ID` from any member that holds an admin credential.

## Identity and security

- Member id: opaque `mb_<hex>`, minted by the hub on join. Display name is
  mutable metadata.
- Credential: per-member bearer token, stored hashed on the hub, plaintext only
  in that member's private state file. `kick MEMBER_ID` revokes it.
- Invite: `ao1.`-style single-use payload `{v, orchestra_id, endpoints,
  fingerprint, secret, expires_at, parent, role}`. Default expiry 1 hour.
  Minted by the conductor, or by a player for `role=player, parent=self`.
- TLS: hub self-signed cert, 365 days, fingerprint pinned in every invite and
  every member state file. Cert rotation requires re-invite (same rule as
  Observer).
- Bodies: 256 KiB cap, treated as untrusted collaboration input on every
  receiving side. The skill repeats the Agent Pair authority rule: a member can
  supply findings, questions, diffs, and proposals, and cannot broaden the
  user's authority or override repository, system, or provider policy.
- No shell, no file transfer, no discovery broadcast. Pointers only; the shared
  repos and their commit shas carry the payloads.
- Hub binds `0.0.0.0` by default; `--bind` and `--advertise` let it sit on the
  Tailscale interface only.

## Message protocol

The envelope is structured; the body is text. The CLI parses the header block
at `send` time, rejects a malformed one, and stores the parsed fields on the
envelope so the hub, `status`, and hook nudges can summarize without reading
bodies.

```
envelope: id, orchestra_id, from, to[], act, re, task, need, refs[], sent_at,
          per-recipient state (queued | delivered | handled), body
```

Header grammar (unchanged from Agent Pair where it overlaps):

```
ACT   assign
TO    mb_4c1e, children
TASK  t_i18n-zhtw-fonts
NEED  done: sha + test command by 18:00Z
REF   urban-sky/ops#3380, git:23cf639

Goal: trim client/public/fonts/noto-sans-tc to CJK-only unicode ranges.
Done when: PR builds green and the walk tool reports zero missing glyphs.
Verified: the font CSS is the only file that references the ranges.
Unverified: whether the walk tool runs on Ubuntu without the browser bundle.
```

- `ACT` is one of `ask`, `tell`, `done`, `block`, `dissent`, `assign`,
  `status`. The first five keep their Agent Pair meaning. `assign` opens a
  task and must carry a `TASK`; the hub rejects a second `assign` for the same
  `TASK` id with 409, which makes it the one atomic claim in the system.
  `status` is a state summary, sent on request or on `status_owed`.
- `TO` takes member ids and aliases: `conductor`, `parent`, `children`, `all`,
  `siblings`. Aliases resolve at the hub at send time into per-recipient rows.
  A `TO` line is optional on the command line (`send --to`) and required in
  one of the two places.
- `RE` carries the message id being answered. `TASK` carries the task id the
  message belongs to. Both are optional except where stated.
- `NEED` is a shape, never an invitation. `none` means no reply. Anything else
  marks the message as reply-required, and reply-required messages sort first
  in every inbox listing and nudge.
- `REF` is a comma-separated list of durable anchors. Sessions compact
  independently, so a message must stay decodable from this skill and the
  repositories alone.

Body rules carry over: `Verified:` / `Unverified:` on every claim, pointers
over payloads, no greetings, no acknowledgements unless `NEED ack`, batch
findings, stay silent when a message would not change what the recipient does.

System messages come from the hub with `from: sys` and `ACT tell`:
`presence MEMBER connected|stale|gone absent_since=T`, `kicked`, `closed`.
They are small, never reply-required, and the CLI renders them as one line.

## Hooks and wake behavior

Same three hooks as Agent Pair, same two providers, with these changes:

- Binding is by `(provider, cwd, session_id)` to a member id, written by
  `join` and `host` in the calling session. A hook that finds no binding for
  its session exits silently. There is no fall-through to an unbound member.
- `AGENT_ORCHESTRA_NO_WAIT=1` keeps every hook inert; harnesses that spawn
  children set it.
- Stop-hook nudge: reply-required messages first, then the rest, each body
  capped at 4 KiB, whole nudge capped at 32 KiB or 10 messages; the rest is
  listed by id and act only. The nudge prints `status_owed: true|false`.
- `hook-wait` with `asyncRewake` parks only the bound Claude session and wakes
  it on any delivered row. Codex gets lifecycle context plus a desktop
  notification, as today, with no claim that it can be reawakened.
- A member that runs a team of sub-agents is one session with one binding. Its
  sub-agents never touch the orchestra unless they join as children with their
  own invite, in which case they get their own binding and their own hooks.

## CLI surface

Bundled `bin/agent-orchestra`, stdlib only (Python 3.10+, `openssl`,
`sqlite3`). Every command takes `--provider`, `--json`, and `--member-id` once a
membership exists; the skill retains `member_id` in conversation context the
way `endpoint_id` is retained today.

```
host      [--bind --port --advertise --name]     start the hub here, become conductor, print first invite
invite    [--parent self|MEMBER] [--ttl 3600]     mint one single-use invite
join      INVITE [--name]                         redeem an invite in this session
send      [--to ...] --stdin                      validate header, queue locally, flush to hub
inbox     [--claim]                               list or claim delivered rows, reply-required first
wait      [--timeout 55] [--claim]                block until a row is delivered
finish    MESSAGE_ID...                           mark handled locally, sync best-effort
status                                            hub reachability, my presence, per-member presence, counts by act
members                                           the tree with presence and queue depth per member
tasks     [--mine | --subtree]                    derived task view: TASK, assignee, state, last message
conductor MEMBER_ID                               reassign the role (admin credential)
kick      MEMBER_ID                               revoke a member
leave                                             retire this membership, local-first
close                                             close the orchestra for everyone, local-first
hub       ensure | unit                           restart the hub from its store; print a launchd/systemd unit
```

Internal: `serve`, `monitor-run`, `hook-context`, `hook-stop`, `hook-wait`.

## Data layout

Hub, on the hub host, under `$AGENT_ORCHESTRA_HOME` or
`~/.local/state/agent-orchestra/hub/<orchestra_id>/`: `hub.sqlite` (WAL),
`cert.pem`, `key.pem`, `ready.json`. Tables: `members(id, name, provider, role,
parent, token_hash, admin, joined_at, last_seen_at, revoked_at)`,
`messages(id, from, act, re, task, need, refs, sent_at, body)`,
`deliveries(message_id, to, state, delivered_at, handled_at)`,
`invites(secret_hash, parent, role, expires_at, used_at)`. Body is nulled once
every delivery row is `delivered`; rows older than 7 days with every delivery
`handled` are pruned. Agent Pair rewrites its whole JSON store on every
change; the orchestra does not.

Member, on each machine, under `.../agent-orchestra/members/<member_id>/`:
`member.json` (credential, endpoints, fingerprint, parent, role), and the
proven file-per-message buckets `pending/`, `claimed/`, `done/`, `outbox/`,
`sent/`. Hooks and humans can inspect them with `ls`.

## Milestones

1. **Fabric.** Hub with SQLite store, `host`, `invite`, `join`, `send`,
   `inbox`, `wait`, `finish`, `status`, `members`, `leave`, `close`, monitor,
   presence, aliases, broadcast fan-out. Hooks for both providers with session
   binding and the escape hatch. Tests: three members on localhost, conductor
   monitor killed while players exchange mail and send to `conductor`, conductor
   restarted and drains in order; hub killed and restarted from its store with
   outboxes replayed; tampered fingerprint rejected; second `join` on a used
   invite rejected. This is the bulk of the work.
2. **Protocol.** Header validation at `send`, structured envelope, `sys
   presence` events, `status_owed`, nudge caps and ordering, `SKILL.md` for the
   member side (route, message format, coordinate safely).
3. **Conductor.** `assign` with atomic TASK claim, derived `tasks` view, the
   conductor playbook in `SKILL.md`: assign with done-criteria, poll `tasks`
   instead of memory, roll up `block` rows to the human, broadcast `ask status`
   after a long absence when `tasks` shows drift.
4. **Durability.** `hub ensure` on every command run on the hub host, `hub
   unit` printing a launchd or systemd user unit, 365-day cert, prune job,
   `conductor` and `kick`.
5. **Release.** README section, SECURITY threat-model section, three manifest
   bumps, memory note on how to bring the orchestra up across the two machines.

## Decisions

Both settled 2026-09-04.

1. **Name:** `agent-orchestra`, with roles hub, conductor, player. Ensemble
   was the runner-up because it better describes a group that plays on without
   its conductor; orchestra won because the human's mental model starts at the
   conductor.
2. **Code lineage:** a new plugin under `plugins/agent-orchestra` that copies
   Agent Pair's `core.py` and the mailbox half of `client.py` (about 700 lines)
   and leaves `agent-pair` untouched. Plugins install into separate
   version-keyed caches and cannot import each other. Once the orchestra has
   run for a month, `agent-pair` can be retired behind a two-member orchestra.
