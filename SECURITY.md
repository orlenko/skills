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
excerpts, and explicit finding evidence. It does not retain every message body
in the observation ledger. The state directory is mode `0700` and the SQLite
database is mode `0600`; it should not be shared between OS users.

Removing a project deletes its active Observer-owned rows and checkpoints but
is not forensic erasure. SQLite pages, filesystem snapshots, provider-owned
logs, and backups may retain bytes. The current CLI has no web listener,
worker-message channel, session-resume action, or semantic analyzer.

## Agent Pair threat model

Agent Pair is intended for two mutually expected agents on one machine or a
trusted/reachable network. The invite carries a single-use join secret and a
SHA-256 certificate fingerprint. Clients disable public-CA validation only
after pinning that exact temporary certificate.

The peer is still an untrusted source of instructions. Plugin hooks expose only
message counts and a retrieval command, never message bodies. The skill directs
agents to treat bodies as collaboration input that cannot expand user
authorization or override system, repository, or provider policy.

Runtime tokens, certificates, queues, and inboxes are stored in a user-private
state directory. Message bodies are removed from the host queue after durable
delivery and from the receiver's active inbox after they are marked handled.

This initial release has no internet relay, identity provider, file transfer,
shell execution, discovery broadcast, or multi-peer rooms.
