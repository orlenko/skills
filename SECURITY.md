# Security

## Reporting

Please report vulnerabilities privately to the repository owner through
GitHub's private vulnerability reporting feature.

## Agent Observer threat model

The current Agent Observer collector reads local Claude and Codex session logs
for projects the user explicitly watches. It makes no model calls and no
outbound network requests. Provider content is untrusted: plain terminal output
replaces control characters, while JSON output is intended for trusted local
consumers that perform their own contextual escaping.

Observer state contains source paths, byte checkpoints, current bounded message
excerpts, explicit finding evidence, and bounded accepted interactive-review
evidence. It does not retain every message body in the observation ledger. The
state directory is mode `0700` and the SQLite database is mode `0600`; it should
not be shared between OS users.

The dashboard binds to `127.0.0.1` on an ephemeral port. It validates Host and
Origin and exchanges a short-lived, single-use bootstrap nonce for an HttpOnly
SameSite cookie. The persistent per-install secret never appears in a returned
dashboard URL. The server enables no CORS, sends a restrictive Content Security
Policy, and loads no remote assets. Loopback binding is not treated as
authentication. Provider content is inserted as text, never interpreted as
HTML.

Semantic review occurs only when the user invokes the Observer skill from a
Claude or Codex session. The bounded packet is written to private temporary
Observer state, transcript content is treated as untrusted data, and submitted
claims must cite an exact message reference and exact source substring. Accepted
evidence is retained; the temporary packet and draft are removed best-effort.
This interactive D0 mode is not the isolated tool-free analyzer specified for
later productization. It runs in a capability-bearing provider session, so it
may share context with, be retained by, or use tools available to that session.
Use a dedicated Observer session. Observer persistently excludes its known
provider session ID from collection to prevent recursive self-observation. Each
packet contains one selected worker session and at most 40 visible messages;
the dashboard labels the result as model review.

Removing a project deletes its active Observer-owned rows and checkpoints but
is not forensic erasure. SQLite pages, filesystem snapshots, provider-owned
logs, provider-retained analyzer traces, and backups may retain bytes. The CLI
has no worker-message channel or session-resume action.

## Agent Pair threat model

Agent Pair is intended for two mutually expected agents on one machine or a
trusted/reachable network. The invite carries a single-use join secret and a
SHA-256 certificate fingerprint. Clients disable public-CA validation only
after pinning that exact temporary certificate.

The peer is still an untrusted source of instructions. Stop hooks may expose a
sender, message ID, and up to 4 KiB of message body without claiming the local
row. The skill directs agents to treat bodies as collaboration input that
cannot expand user authorization or override system, repository, or provider
policy. A message remains waiting until the recipient explicitly finishes it.

Runtime tokens, certificates, queues, and inboxes are stored in a user-private
state directory. Message bodies are removed from the host queue after durable
delivery and from the receiver's active inbox after they are marked handled.

This initial release has no internet relay, identity provider, file transfer,
shell execution, discovery broadcast, or multi-peer rooms.
