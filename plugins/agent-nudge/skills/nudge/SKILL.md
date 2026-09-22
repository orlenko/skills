---
name: nudge
description: "Run agent-nudge on this machine: a daemon that finds Claude Code and Codex sessions in tmux that sit idle after their turn, and types one polite question into each: is your goal done, or are you blocked? Use when the user wants to install, start, stop, check, or switch the nudger between dry-run and live, see what it nudged, or opt a tmux pane out."
---

# Agent Nudge

Use the bundled `bin/agent-nudge` executable. Resolve its absolute path from
this skill: the plugin root is two directories above this skill directory.

Agents often stop short of their goal. They report and stop, or they say they
will act when a CI job finishes and set nothing up to wake them. One outside
line ("still waiting?") usually restarts them. The nudger sends that line to
tmux panes that run `claude` or `codex`, one daemon per machine.

## What it does, and when it holds back

Every 30 s it reads each agent pane's screen. It types into a pane only when
all of these hold:

- The screen above the input box has not changed for 10 minutes
  (`AGENT_NUDGE_IDLE_MINUTES`). A monitor or background task in the footer
  stretches that to 60.
- The input box is empty. Dim autosuggestions count as empty; anything a person
  has typed does not.
- No attached tmux client in that session has had keyboard activity in the last
  5 minutes (`AGENT_NUDGE_HUMAN_QUIET_MINUTES`).
- There is no spinner and no dialog.
- TypeSafe Jev reads the screen as idle, and the last message is not a question
  for the user.

It nudges once per stop. Before a repeat, Jev reads the visible conversation
and receives trusted continuity facts from the daemon: the unchanged duration,
the number and elapsed span of consecutive nudge/reply cycles, and whether any
non-nudge wake interrupted them. It decides whether asking again could
plausibly cause useful work or a useful blocker report. An explicit terminal
answer such as "the goal is done; nothing is pending" leaves that unchanged
pane quiet. When another nudge still makes sense, the next wait triples, up to
4 hours. Anything else that wakes the agent, a person or mail, resets the wait.
There is a cap of 12 nudges per pane per day.

When Jev reads the last message as waiting on something outside the agent,
and nothing in the footer is watching for it, the nudge says so and asks the
agent to check now or to set up a watch. Every other nudge asks whether the
goal is done or blocked. Nudges start with `[agent-nudge]`, so an agent never
takes one for its user.

When an agent answers a nudge by naming an obstacle and stopping ("blocked on
the engine defect", "waiting for #73 to merge", "asked the conductor for a
ruling; if you'd rather I just go, say so"), the next nudge in that chain
pushes back once: can you do something about it? Check recent PRs for
someone else's fix, ask your conductor, manager agent or pair partner, or fix
it yourself in a PR stacked under your work. If it truly needs a person, name
the decision. This goes out even when the reply was a permission question or
the player has no open task. A wait on the user ("waiting on your recording")
doesn't count, and neither does a denial ("nothing is blocked"). It's a phrase
match on the reply, because Jev couldn't separate these cases on real screens.

## Agent Orchestra players

When a pane's agent holds an Agent Orchestra seat, the nudger reads that
member's files. The seat's `owner_pid` is the agent process in the pane.
- Mail that reached the member's local inbox after the pane went still means
  the session didn't hear its wake-up. The nudge then comes after 2 idle minutes
  instead of 10, even if the last message asks the user something, and says how
  many messages arrived. Mail older than the stop was already shown to the agent
  and doesn't count.
- A player with no open task and no new mail is idle by design and is not
  nudged. The conductor assigns work. The conductor itself is nudged like any
  other session.
- A nudge to a player with open tasks names them.

## Commands

- `status [--json]`: mode, whether the daemon runs, whether the judge is on,
  and the last 24 hours. `resumed_work_after_nudge` counts nudges followed by
  at least two minutes of work.
- `panes [--json]`: the agent panes it sees and what each screen shows.
- `log [-n 30] [--hours 24] [--json]`: recent decisions.
- `mode [dry-run|live]`: show or set the mode. It starts as `dry-run`, which
  logs what it would send and types nothing.
- `install` / `uninstall`: the launchd (macOS) or systemd `--user` (Linux)
  service. Run `install` again after a plugin update, because the service
  points at the plugin copy that installed it.
- `run --once`: one pass in the foreground, printing what it logged.

## Setup

1. The judge needs `TYPESAFE_API_KEY`. A service starts with a bare
   environment, so put `TYPESAFE_API_KEY=...` in `~/.config/agent-nudge/env`
   (mode 600). Never copy a key into a repository. Live mode refuses to type
   without the judge. Dry-run works without it.
2. Run `install`, then `status`.
3. After a day or so in dry-run, read `log`. If its choices look right and the
   user agrees, run `mode live`. Switching to live is the user's decision: ask,
   do not assume.

To keep a pane out of it: `tmux set-option -p -t <pane> @nudge off`.

## Privacy

With the key set, the last ~40 lines of an idle agent's screen go to TypeSafe,
a third party, once per stop. The log in `~/.local/state/agent-nudge/`
(mode 600) keeps the screen tail of every nudge so the choices can be
reviewed.
