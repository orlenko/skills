# Agent Pair and the other agent-to-agent transports

`agent-pair` was written before two other ways of moving text between agent
sessions were on our radar: [`wire`](https://github.com/SlanchaAI/wire), and
Claude Code's own cross-session messaging. This note records how they compare,
what each one takes off `agent-pair`'s plate, and what `agent-pair` is now for.

The short version: the transport layer is squeezed from both sides, while the
layer that says *what to put in a message* is uncontested by either. That is why
the message protocol moved out into the
[`peer-message`](../plugins/peer-message) plugin, and why `agent-pair` now
leads with the cases the other two cannot serve.

## Claude Code cross-session messaging

Shipped in Claude Code v2.1.224, on by default, macOS and Linux only. Two native
tools — `ListAgents` to discover reachable sessions and `SendMessage` to
deliver text to one by name. Same-machine delivery runs over a per-session Unix
socket and never reaches Anthropic servers. Sessions register themselves in
files on disk; each binds an inbox socket, exported to hooks as
`CLAUDE_CODE_MESSAGING_SOCKET` and shown in `/status` as `Peer address`.

This is the direct overlap, and it takes the most common `agent-pair` case
outright: two Claude Code sessions on one machine now message each other with no
plugin, no invite, and no monitor process.

What it does not cover:

- **Claude only.** It cannot reach a Codex session, and nothing about it
  suggests it ever will.
- **Reply-only across machines.** A session on another machine is reachable only
  through Remote Control, only as a reply to a message that arrived from it, and
  the message travels through Anthropic servers.
- **No durable mailbox.** A session appears only while it is running and bound
  to its socket. Held messages cap at 100, accepted-but-unread messages cap at
  50 per session, and an unanswered approval dialog drops the message after
  `dialogExpiry`, five minutes by default. A message for a peer that is not up
  has nowhere to land.
- **No work-item lifecycle.** Plain text, fire and forget. No queued, delivered,
  or handled state, and no claim-and-finish inbox.
- **Filesystem-scoped discovery.** Peers find each other by reading shared
  files, so a session inside a container and a session on the host cannot reach
  each other. Two sessions inside the same container can.
- **Off in some configurations.** Not on native Windows, not on Bedrock,
  Claude Platform on AWS, Google Cloud's Agent Platform, or Microsoft Foundry,
  and it stays off when `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
  `DISABLE_TELEMETRY`, `DO_NOT_TRACK`, or `DISABLE_GROWTHBOOK` disables the
  feature-flag evaluation it depends on.

Its inbound controls (`crossSessionInbound` set to `accept`, `hold`, or
`refuse`, with an approval dialog on hold) and its framing of a peer message as
something that cannot approve a permission prompt or change configuration match
the stance `agent-pair` already took. That is corroboration, not a reason to
change course.

Agent teams are a separate, still-experimental feature behind
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`: a lead session spawns teammates that
share a task list. It overlaps `agent-pair` far less than cross-session
messaging does, because the teammates are spawned and supervised rather than
independently started and paired.

## wire

`wire` is a federated signed-message bus: Ed25519 identity resolving to
`did:wire:<handle>`, agent cards, trust tiers, a bilateral dial-and-accept
consent gesture, append-only signed JSONL events with Nostr-compatible kinds, an
HTTP relay with a Nostr fallback transport, org attestation over DNS-TXT, and an
MCP server. At the time of writing it is v0.17.0, pre-1.0, roughly 55k lines of
Rust against 321 crates, under a three-license split with the relay server under
AGPL-3.0.

It solves a problem `agent-pair` does not: reaching a peer across NAT,
organizations, and networks, with a durable identity that outlives any single
pair. `agent-pair` needs the host to be directly reachable, which is why the
skill documents `--advertise`.

What it does not cover:

- **No message-content protocol.** `wire`'s own skills document how to call
  `wire send`; nothing in its docs says what to put in the body. Per its
  `ANTI_FEATURES.md` it deliberately refuses to be a collaboration or
  orchestration layer, so this is a permanent gap rather than a missing feature.
- **No per-message work lifecycle.** An append-only event log with a read
  cursor, not a mailbox with claim, finish, and handled states.
- **Weight.** A Rust binary and a background daemon, against `agent-pair`'s
  Python 3.10 plus `openssl` and no third-party dependencies.

## Where that leaves agent-pair

Reach for `agent-pair` when at least one of these holds:

1. **Cross-provider.** A Codex session and a Claude session working together.
   This is now the clearest reason the plugin exists.
2. **Durability.** The message must wait in a durable inbox for a peer that is
   closed, restarted, or not up yet, and the sender must be able to finish and
   close without the peer present.
3. **Boundaries.** Container to host, or machine to machine on a LAN, with no
   third party in the data path and either side able to initiate.
4. **Lifecycle.** Delivery states and a claim-and-finish inbox, rather than
   fire-and-forget text.

Two Claude Code sessions on one machine are no longer on that list.

## Open questions

- A rendezvous or relay fallback would remove the one hard limit `agent-pair`
  has against `wire`: two peers with no directly reachable address between them.
  The `ap1.` invite could carry a rendezvous address while keeping the pinned
  certificate, so the relay stays dumb transport rather than a trusted party.
- Keying an endpoint off the host's session id, with the working directory as a
  fallback, would remove the retained `--endpoint-id` ceremony the skill
  currently carries. Both `wire` and Claude Code arrived at per-session
  identity; `wire` documents an ugly failure mode from getting it wrong.
- `status --json` could report an ambiguous local state — several endpoints in
  one directory with no `--endpoint-id` given — rather than quietly picking
  one.
- There is no documented way to mute the best-effort desktop notifications.
