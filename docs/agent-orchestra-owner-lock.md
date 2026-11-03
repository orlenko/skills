# Ownership lock lifetime correction

The existing single-writer regression failed in PR CI `41582573141`: two
claimants wrote one orphan's owner record. The quality-setup change had not
modified plugin code. `acquire_pid_lock` published an empty pathname before its
JSON owner record; a contender treated unreadable metadata as a dead PID and
unlinked the active reservation. Separate stale readers could also unlink a
replacement pathname. A passing parallel run does not resolve either race.

## Decision before implementation

Reuse the existing monitor lock's nonblocking, descriptor-owned `flock`
mechanism through one small path-based context. Never unlink the persistent
coordination inode. Convert the four internal PID-lock callers to a context
instead of maintaining a process-global descriptor registry.

Keep two paths with different responsibilities:

- `<legacy-path>.guard` is the persistent kernel coordination inode.
- The existing PID JSON path is transient activity/compatibility metadata.

Each acquisition opens a fresh non-inheritable descriptor. Contention returns
the existing busy result; other syscall errors never grant unlocked success.
After acquiring the guard, honor a valid unmarked live legacy PID record.
Dead or malformed remnants are replaceable under the guard, preserving the
existing stale-metadata recovery behavior. New metadata carries a protocol mark
and unique acquisition token, and is atomically published. Once the guard is
free, marked metadata is stale even if its PID still lives: an exception must
not strand the operation for that process's lifetime.

Hold the guard through member reload/decision/save, the session update or the
entire parked waiter, including transient-marker cleanup. Cleanup removes only
the matching acquisition token. Descriptor closure is unconditional even if
publication or cleanup raises. Nested acquisition contends; it cannot release
the outer context. `adopt` still releases its owner context before reclaiming;
the waiter may hold its distinct guard while briefly acquiring the owner guard.

The monitor retains its current integer-PID format and `owner`/`hold` behavior.
Only its flock mechanism is shared; legacy JSON policy belongs to member locks.
Member records, invites, wire protocol and CLI behavior do not change.

## Independent design critique and disposition

A separate Design Skeptic advisor inspected the concrete proposal and actual
callers before source implementation. Its recommendations are incorporated:
preserve waiter-marker appearance/removal and valid live legacy records; separate
the persistent guard from metadata; use unique cleanup tokens; disregard stale
marked metadata when the guard is free; keep descriptor ownership in contexts;
and preserve monitor format and original single-writer assertions. This is a
pre-implementation advisory critique, not finished-diff clearance.

The smaller alternative of merely atomically writing a PID file still leaves
stale-reader unlink races. A path-to-descriptor registry adds unnecessary alias,
thread, reentrancy and release-ownership rules. Two context layers reuse the
existing primitive while preserving the marker contract.

## Effects and limits

The change touches only owned runtime coordination/metadata files. A completed
operation may leave a small guard file; its existence is not a live owner. The
kernel releases ownership after process death. Ordinary executed children must
not inherit the descriptor. A fork-only child can inherit an open description;
the inspected plugin has no such launch route, and this change makes no
fork-proof or hostile-filesystem guarantee.

A well-formed old live PID record is respected. An old executable does not
participate in the new guard protocol, so its own partial-publication/stale-unlink
races remain possible in mixed-version operation. Source integration does not
qualify a runtime transition or restart installed monitors. The existing monitor
lifetime protocol is unchanged. No release, version change, installation,
provider call or runtime restart belongs to this correction.

## Acceptance evidence

Use deterministic contention while publication is paused, same-process and
separate-process exclusion, exception/dead-process reacquisition, marked stale
metadata with a live PID, stable guard inode, nested contention, matching-token
cleanup, legacy-live refusal and executed-child descriptor closure. Preserve the
original seat single-writer assertion and waiter marker/lifetime tests. Run the
current complete CI matrix and obtain independent finished review before
integration; a targeted local result is not that matrix.
