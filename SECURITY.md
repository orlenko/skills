# Security

## Reporting

Please report vulnerabilities privately to the repository owner through
GitHub's private vulnerability reporting feature.

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
