---
name: design-skeptic
description: Pressure-test an idea, proposal, or architecture before implementation through separate skepticism and simplicity lenses. Use when Codex or Claude is choosing an approach, evaluating a rewrite, migration, service, framework, or pattern, or challenging a plan's assumptions, hidden costs, failure modes, and smallest viable form. Keep the review advisory; do not use it as a finished-diff or code-review workflow.
---

# Design Skeptic

Pressure-test a concrete design before it hardens into code. Surface risks and
offer a smaller viable path; never block the user's decision.

## Resolve the target

- Treat inline text as the proposal.
- Read a supplied design or plan path in full.
- With no explicit target, use the proposal currently under discussion.
- If no concrete proposal exists, ask for a short description.
- If the target is a finished diff, decline this lens and recommend an
  appropriate code-review workflow instead.

## Run an independent pass

Resolve the plugin root two directories above this skill directory. The full
advisor persona is at `agents/design-skeptic.md` under that root.

Prefer an independent advisor when delegation is available:

1. If a registered `design-skeptic` subagent exists, dispatch it with the
   target and the relevant repository areas.
2. Otherwise, if generic subagents are available, launch one background
   subagent. Tell it to read the advisor persona in full, inspect the actual
   code and decisions, and return the prescribed critique. Provide the target,
   relevant paths, and constraints. Keep the task read-only unless the user
   separately authorized edits.
3. If delegation is unavailable, read the advisor persona and apply it
   yourself.

When the user explicitly requests background work, do not wait for the advisor
before returning control. Report that it is running and retain enough context
to collect its result later.

## Ground the critique

- Inspect real implementation and local conventions before asserting how the
  system works. Use `rg` for searches spanning multiple files.
- Check prior decisions when they are available. Run
  `~/.rationale/repo/bin/rationale` only if that exact path is executable;
  otherwise skip it quietly. Useful forms are `search --files <path>` and
  `search "<keyword>"`.
- Distinguish evidenced facts from inferences. Cite real primary sources only
  when they materially support a risk; never invent authority.

## Relay the result

Preserve this order:

1. **Framing** — the actual problem in one or two sentences.
2. **Sharp edges (skeptic lens)** — assumptions, hidden costs, and concrete
   failure modes.
3. **The simpler path (simplicity lens)** — the smallest version that ships
   value and what to delete, defer, or hardcode.
4. **Recommended approach** — constructive sequencing and explicit trade-offs.
5. **Pre-mortem** — the top three to five concrete reasons it failed six
   months after shipping.
6. **References** — optional, verified, and relevant.
7. **Aside** — optional and limited to one useful line.

Relay an independent advisor's findings without sanding them down. If you
disagree, preserve the finding and add your disagreement with evidence. Make
clear that the critique is advisory and the user decides.
