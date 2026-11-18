# Herdr collaboration sidebar

## Goal and acceptance

Show Orchestra and Pair participants together in Herdr even when their terminals
live on different machines. Clicking a participant focuses his existing terminal.
Machine identity remains visible. Messaging, membership and authorization remain
owned by Orchestra and Pair. No hub credentials, invites, message bodies or new
network service enter the mod.

This is a Tier B change: it touches member/client lifecycle paths. The repository's
round-1 question is: what lets another machine or session read, inject or run
something it should not? Development uses an isolated worktree; no live membership,
application configuration, monitor or terminal is modified during verification.

## Design

1. Each independently installed plugin carries a small standard-library-only
   Herdr display publisher. Keep the shared wire format and publisher identical
   across packages and check parity in tests. Herdr is optional; absent or failing
   Herdr must never fail an existing plugin operation.
2. Use disjoint per-publication labels under `orlenko.orchestra.` and
   `orlenko.pair.` (participant hash plus random publication token). Multiple
   relationships of the same kind on one terminal are supported and cannot
   overwrite each other. Each JSON value is explicitly versioned. It holds opaque group
   and participant ids, bounded display names, role, a lease expiry, and terminal
   incarnation identifiers. No secrets, socket paths, process arguments, workdirs,
   conversation text or provider account information are published.
3. An owning CLI/hook captures the terminal from HC_TERMINAL_SOCKET and
   HC_TERMINAL_ID, verifies the local Unix peer and terminal/process association,
   and stores its versioned private display binding. The monitor renews a short
   lease only while that exact terminal and owning process still exist. Never
   pick a terminal by directory, title, name, or machine alias. Never use HC_SOCKET
   as an authority to discover the owner. Bound all socket operations and replies.
4. Labels use exact local terminal addresses; Herdr qualifies them for its viewer.
   Stable group ids join participants across machine aliases. Herdr labels are
   untrusted presentation data, never membership proof or authority for messages.
5. Join/host/accept and owning hooks publish; monitor passes renew. Adoption and
   quota rollover rebind from the new owner. Leave/close/kick/expiry clear only a
   label that still belongs to the same participant and publication. When cleanup
   cannot be proven safe, leave the short lease to expire. Do not erase another
   publication or the other plugin's label. Replaced processes and closed or
   reused terminals cannot keep renewing a previous relationship.
6. Names/roles come from local membership plus the existing authenticated roster
   refresh. Publishing must not introduce an extra network request. Label writes
   are best effort. Each participant sidecar has a bounded nonblocking file lock;
   invalidation runs inside authoritative ownership/close transitions, before
   rebinding. Monitor refresh reads current authoritative state under that lock
   and cannot capture or recreate a retired publication. An old publisher only
   removes its disjoint key, never a successor key. Missing-session/cwd-only Pair
   hooks cannot establish display ownership. Remote Pair closure durably retires
   display publication so later hooks cannot resurrect it.
7. Publication requires deliberate per-host/session opt-in via
   `ORLENKO_HERDR_DISPLAY=1`; `0` disables publication from an owning lifecycle
   action. Existing private enablement survives a monitor restart and account
   adoption, with no new opt-in inferred from viewer installation. Names and
   relationship ids become readable by every authorized client of that Herdr
   core; this audience is documented. No metadata is published by default.
8. A standalone `integrations/herdr/collaborations/` mod reads Herdr terminals and
   labels, validates the bounded versioned data, and groups by relationship kind
   and opaque group id. Display names are plain text. Use the app's returned
   machine-qualified terminal address for navigation; never interpolate a name
   into shell commands. No exec, fetch, hub client or credential access in the UI.
9. Group headings say Orchestra or Pair (or a published or locally chosen name)
   with a member count; member rows are one line of name, role, machine and
   terminal activity. A Pair row is named by Herdr's terminal title. Duplicate names
   remain separate. A terminal may appear in both relationship kinds. Sanitize display strings to bounded single-line text, removing control and bidi
   override characters. Ignore expired, malformed, oversized, far-future,
   unknown-version and incarnation-mismatched labels. Navigation never trusts a
   label-provided target; machine badges come from the Herdr terminal address.
10. Groups are a navigation view of visible, connected terminals, not a complete
   roster. The section heading's tooltip explains that a missing terminal is not proof a member left.
   Re-resolve address and incarnation at click time; focus an existing pane,
   otherwise use `terminal.peek`. Do not infer a complete roster from visible rows.
11. Use theme colors and normal Herdr focus/keyboard behavior. Update from native
    terminal data changes; use a slow timer only to expire leases. Ship a self-
    contained installer that targets an explicitly supplied Herdr configuration
    home, refuses to overwrite custom edits, and never alters shipped mods or
    layouts. Run it on the viewing machine (e.g. the laptop), not each agent host.
    Installation into a person's running app remains a separate explicit action.

## Increment and validation

- First: independent Astra/high design critique, then one revision of this plan.
- Implement publisher and lifecycle coverage in both plugins, and the shared mod
  and installer. No wire protocol changes to either collaboration transport.
- Publisher tests use private fake Unix sockets, synthetic terminal/process
  identities and isolated state. Cover no Herdr, bounded errors, unauthorized or
  wrong-incarnation targets, rollover/adoption, owner death, lease expiry,
  rename/role refresh, close/kick/expiry, and stale cleanup racing new ownership.
- Mod tests run the pure grouping logic with Node and a small Herdr UI mock:
  cross-machine grouping, duplicate names, malformed/bounded data, expiry,
  incarnation checks, disconnection, dual membership and exact navigation target.
- Installer tests use a scratch Herdr home and cover safe repeat installation,
  refusal of edited files, and preservation of unrelated files.
- Run each touched plugin's focused suite during development; commission a fresh
  finished-diff reviewer with the Tier B question and process v0.3 rubric. Fix
  blockers and request only the permitted fix-diff re-review.
- Run the four CI plugin suites once on the final code with ResourceWarning as
  error. Node tests must run from a touched plugin suite so existing CI covers it.
- A real Herdr app check and visual smoke use a scratch app home and synthetic
  terminals only, if the application binary is available. Otherwise report that
  exact gap; unit tests do not establish rendered native UI compatibility.
- Provide source, install instructions and check evidence on the feature branch.
  No merge, plugin release, live monitor restart or app installation is implicit.

## Non-goals

No replacement for Spaces, no cross-machine native Space, no roster discovery,
message composer, terminal input, automatic enrollment, remote configuration,
task board, hub credential distribution, or alteration of Pair/Orchestra access.

## Design critique disposition

Astra/high completed one independent critique before implementation. All four
blockers were accepted: disjoint publication keys eliminate cross-participant
slot contention; locked invalidation/current-state refresh fences handoffs and
closure; host-side opt-in and viewer validation bound metadata exposure; and
click-time incarnation checks plus pane/peek navigation cover non-pane agents.
Focused tests will exercise the full classes of these scenarios.
