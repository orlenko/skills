---
name: undrudge-apply
description: Clear exactly one open undrudge recommendation belonging to the repository this session is already working in. Use when the user asks what undrudge has queued here, asks to apply, action, or clear an undrudge rec, or names a rec id to fix. Requires the undrudge CLI and a git repository. Report and stop instead of implementing when the working tree is dirty. Do not use for dispatcher briefs under .undrudge-inbox, and do not use for cross-cutting or agent-global recs.
---

# Undrudge Apply

undrudge is a background watchman that mines shell history and agent transcripts
for automation opportunities. Its recommendations pile up because acting on one
normally means abandoning whatever the session is doing. This skill is the pull
side: from inside a session that is already in the repository, take **exactly
one** recommendation and close it out.

One invocation disposes of at most one recommendation. Re-running takes the
next. Do not batch, do not clear a second rec while already here, and do not
open two pull requests.

## Preflight

`undrudge` must be on `PATH`. If it is not, say so, point at
`https://github.com/orlenko/undrudge`, and stop; do not attempt to install it.

The session must be inside a git repository. `undrudge here` exits 2 with an
`error` key when it is not. Report that error verbatim and stop.

## Find the work

```bash
undrudge here --json
```

That single call returns the repository's identity, whether its tree is dirty,
and the open recommendations that belong here, best match first. It takes a few
seconds on a large store, so call it once per invocation and never in a loop.
`--limit 0` returns every match; `--dir PATH` anchors elsewhere.

Each candidate carries `id`, `id12`, `title`, `tier`, `body_path`,
`created_at`, and `evidence_cwds`. Read the recommendation from `body_path`
directly. Tiers, strongest to weakest:

- `this_clone` — evidence was observed in this working tree.
- `same_repo` — evidence came from a sibling clone of the same origin. Several
  checkouts of one repository are one repository.
- `named` — no evidence points here, but the recommendation's title names this
  repository. This is the tool-fix case: the pain is felt wherever the tool is
  used, while the fix belongs in a source tree that appears in no evidence at
  all. Treat it as a hint and verify harder than usual before building on it.

When the user supplies a rec id, skip the query and work that recommendation
directly. `undrudge show ID` prints the absolute path of its markdown file, not
the body; read the file at that path. A unique id prefix is accepted everywhere
a full id is.

With no candidates, say so plainly, report how many recommendations were
considered, and stop. When `unreadable` is non-zero, say that coverage was
incomplete.

## Refuse a dirty tree

When `repo.dirty` is true, implement nothing. Branching on top of someone
else's work in progress is not this skill's job.

Still deliver the observation, because losing it is the problem this skill
exists to solve: print the id and title of the recommendation that would have
been picked, state that the tree is dirty and why, and stop. Do not stash, do
not create a worktree, do not offer to clean the tree.

`dirty` fails closed — a `git status` timeout or an interrupted rebase reports
dirty with a reason. Do not second-guess it by running git again.

## Verify before building

Read the top candidate in full, then check it against the live tree. undrudge
evidence runs two to four weeks stale and is occasionally hallucinated, so
verify all four before writing any code:

1. The cited files, commands, and paths still exist here.
2. The pain is steady-state rather than a burst that has already ended; check
   the evidence dates.
3. It is not already solved — check the tree, recent commits, and merged pull
   requests.
4. It does not target a retired stack, and it does not automate a loop the user
   runs deliberately by hand.

If the top candidate fails a predicate, closing it out **is** this invocation's
one action. Dismiss it with a reason and stop; shrinking the pile is progress.
Do not fall through to the next candidate.

## Choose one disposition

| Disposition | Action |
|---|---|
| ship | Implement it, open a draft PR, then `undrudge mark <id12> dispatched --reason "<PR url>"` |
| dismiss-stale | Evidence dead or obsolete: `undrudge dismiss <id12> --reason "..."` |
| reject-as-framed | Real pain, wrong proposal: `undrudge dismiss <id12> --reason "<the better framing>"` |
| already-done | Solved since the recommendation was written: `undrudge implement <id12> --reason "..."` |
| needs-human | Blocked on a decision or a credential: change nothing and state exactly what is needed |

`undrudge mark` takes the id first and the status second. Valid statuses are
`logged`, `dismissed`, `implemented`, `dispatched`, and `rejected`.

Flip the status in this same session. Do not leave it for the dispatcher to
reconcile: the dispatcher may not be running, and a recommendation that was
acted on must leave `logged` immediately or the pile grows back.

## Implement (ship only)

- Branch `undrudge/<id12>-<short-slug>` off the repository's base branch.
- Keep the change minimal and idiomatic to this repository. Match the
  surrounding style, do not reformat neighbouring code, and do not add
  dependencies.
- Run this repository's own checks — its test and lint entry points, or the
  ones named in its `AGENTS.md` or `CLAUDE.md` — before committing. If they
  fail, fix them or fall back to needs-human. Never commit red.
- Commit subject `undrudge:<id12> <title>`.
- Open the pull request as a draft with `gh pr create --draft`. The body states
  what the recommendation observed, what was built, and how it was verified,
  and ends with the attribution line this agent normally appends.
- Return to the base branch so the tree is clean for the next invocation.

## Guardrails

- Never merge the pull request. Draft status is the human approval gate.
- Never modify `~/.zshrc` or any existing dotfile, never touch `.a2a/`
  directories, and never schedule a cron job or a loop.
- One recommendation, one branch, one pull request. A recommendation too large
  for a focused change is needs-human, not a sprawling diff.
- `cross_cutting` and `agent_global` recommendations are out of scope. They span
  directories and have no single target repository; `undrudge here` filters them
  out and `--all-scopes` exists for inspection only. The user triages those by
  hand through `undrudge browse` and `undrudge copy`.
- Dispatcher briefs under `.undrudge-inbox` are a different entry point. Do not
  consume them here.

## Report

Close with one short paragraph: which recommendation, which disposition, why,
and the pull request URL when one was opened. Then say how many other
recommendations remain queued for this repository, so the user knows whether
running again is worth it.
