# Working on Orlenko Skills

Read [README](README.md), [SECURITY](SECURITY.md), the relevant plugin's
instructions, and current Git status/diff before changing source. Preserve
concurrent work and coordinate quality-process changes with the canonical
repository's single setup owner, recorded in the [quality setup](docs/software-quality.md).

## Review and test process

This repository follows the [review and test process v0.3][process]. That text
sets the tiers, stop rules, reviewer report rules and record form. This block
supplies the repository's parts. Only the owner changes this block.

Skills are installed from this repository, so a merge to main ships by itself.
Stop rule 11's merge-on-green exception does not apply here.

**Tier B paths** (source files; test files are not listed):

- `plugins/agent-orchestra/agent_orchestra/{core,hub,member,protocol,keys,podium,codex_wake,hooks}.py`
- `plugins/agent-pair/agent_pair/{core,server,client,codex_wake}.py`
- `plugins/agent-observer/agent_observer/{remote,ingest,web,httpserver,exporter,runtime}.py`
- `plugins/agent-nudge/agent_nudge/**`
- `plugins/*/hooks/**`, `plugins/agent-orchestra/hooks.json`

**Triggers** (a change is B if it matches one):

- Changes what one agent session can make another session do.
- Changes what is written into another agent's config, prompt or hook output.

**Round-1 question:** what lets another machine or session read, inject or run
something it should not?

**Reviewer:** one fresh reviewer that did not write the change: hostile-eyes in
a Claude session, a local Codex review on the `gpt-6-astra` model in a Codex
session. When one is out of quota, use the other. Design Skeptic is for designs,
not finished diffs.

**Tests.** Focused, per plugin:
`python3 -m unittest discover -s plugins/<plugin>/tests -v`.
Full suite, on the final head: the four suite commands in
[`.github/workflows/test.yml`](.github/workflows/test.yml), each run with
`python -W error::ResourceWarning`.

**Smoke checklist:** none yet. The loopback end-to-end tests already run in the
full suite.

[process]: https://github.com/orlenko/software-quality/blob/main/docs/process.md
