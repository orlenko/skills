---
name: undrudge-triage
description: Review the global Undrudge backlog with the user, one finding at a time. Use when the user wants to triage, prioritize, deduplicate, dismiss, defer, or hand off logged Undrudge recommendations. Do not implement findings, create pull requests, or replace the repo-local undrudge-apply workflow.
---

# Undrudge Triage

Use this skill for a conversation about the global Undrudge backlog. Keep the
user in control of every status change and keep implementation in the separate
`undrudge-apply` workflow.

## Load the backlog

`undrudge` must be on `PATH`. Run this command once at the start:

```bash
undrudge list --status logged --json --limit 0
```

If the command fails or does not return valid JSON, report the incompatibility
and stop without changing any recommendation. Do not substitute a narrower
query, scrape human-readable output, or guess at missing findings.

Rank the returned findings by likely value using their observed frequency,
impact, confidence, breadth, age, and apparent cost to fix. Start with the most
valuable-looking finding. Do not dump or decide the whole backlog at once.

## Review one finding

For the selected ID, run:

```bash
undrudge show ID
```

The command prints the path to the full finding. Read that file, then validate
the finding against current reality before recommending a disposition:

- Confirm the cited behavior, paths, commands, or product limitation still
  exist.
- Check whether recent code, configuration, tooling, or process changes
  already solved it.
- Distinguish recurring friction from a temporary burst.
- Check the remaining logged backlog for the same title, signature, rationale,
  and evidence. Daily and weekly findings commonly duplicate one another.

When two findings are duplicates, recommend one canonical finding to retain,
normally the one with stronger evidence or the more precise framing, and
recommend dismissing the other as a duplicate. Read both full findings before
making that recommendation. Never dismiss either without the user's approval.

Summarize the evidence, live validation, value, cost, uncertainty, and any
duplicate relationship. Then ask the user to choose exactly one disposition:
**implement**, **dismiss**, **hand off**, or **defer**.

## Apply only the approved disposition

- **Implement:** Do not begin work and do not create a branch or pull request.
  For a repository-local finding, direct the user to invoke the installed
  `undrudge-apply` skill from that repository with this finding ID. Leave the
  status `logged` until implementation is actually complete. For an
  agent-global or cross-cutting finding, identify the appropriate maintainer or
  source repository and give the user a concise prompt for a separate
  implementation session; include `undrudge show ID`. If current validation
  proves the work was already completed, separately ask permission to mark it
  implemented, then run
  `undrudge implement ID --reason "REASON"` only after approval.
- **Dismiss:** Ask for or confirm the reason, then run
  `undrudge dismiss ID --reason "REASON"` only after explicit approval.
- **Hand off:** Draft a self-contained handoff naming the target, requested
  outcome, relevant evidence, constraints, and the command
  `undrudge show ID`. Creating text is not a confirmed handoff. After the user
  confirms that the handoff was actually sent or authorizes this session to
  send it through an available channel, run
  `undrudge mark ID dispatched --reason "HANDOFF DESTINATION"`. A handoff is
  `dispatched`, never `implemented`.
- **Defer:** Make no status change. There is no deferred status; do not invent
  one and do not use `logged` as a write merely to record the choice.

Treat any other requested status change the same way: explain the exact
mutation and wait for explicit approval immediately before running it. Never
infer mutation approval from agreement with the analysis.

After resolving or deferring the current finding, continue to the next item
from the captured backlog only if the user wants to keep triaging. Revalidate
each finding in full before discussing its disposition.

## Boundaries

- Never create a pull request, branch, commit, patch, or implementation from
  this skill.
- Never mark planned work `implemented`.
- Never mark a proposed or merely drafted handoff `dispatched`.
- Never mutate more findings than the user explicitly approved, including
  obvious duplicates.
- Keep the wording and commands agent-neutral. Do not assume Claude-only or
  Codex-only slash-command syntax.
