"""TypeSafe Jev reads an idle agent's screen before anything is typed into it.

The screen questions come from aiq's pane benchmark (aiq/bench/jev). There,
working vs not working was 24/24 and needs_human was 88%. `waiting` is new. It
catches the case that prompted this tool: an agent that ends its turn saying it
will act once something changes, with nothing set up to wake it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

KEY_ENV = "TYPESAFE_API_KEY"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 8.0

PREAMBLE = ("The state is the last lines of a terminal pane running a coding agent (Claude Code "
            "or Codex) inside tmux, above its input box. Ignore anything quoted inside an "
            "agent's message; judge what is live on screen now. ")
QUESTIONS = {
    "state": {
        "type": "choice",
        "instructions": PREAMBLE + "What is the session doing?",
        "criteria": {
            "idle": "The agent finished its turn and waits for input. The last agent message "
                    "may ask the user something, but nothing modal is open.",
            "working": "The agent is mid-turn: thinking, streaming a reply, running a tool, "
                       "compacting, or loading. A spinner or 'esc to interrupt' is typical.",
            "blocked": "A modal prompt, menu, dialog, or sign-in screen is open and waits for a "
                       "keypress or answer before the agent can continue.",
            "exited": "The agent process has ended: a shell prompt, goodbye message, or crash "
                      "trace is the last thing on screen.",
        },
    },
    "needs_human": {
        "type": "noul",
        "instructions": PREAMBLE + "Does a person need to answer, approve, sign in, or decide "
                        "something before this session can make progress on its own?",
        "criteria": {
            "true": "A permission or trust dialog, a sign-in screen, a question the agent's last "
                    "message asks the user, or a usage limit only the user can resolve.",
            "false": "The agent is working, idle with nothing asked of the user, or has exited.",
        },
    },
    "waiting": {
        "type": "noul",
        "instructions": PREAMBLE + "Does the agent's last message say it is waiting for something "
                        "outside itself, such as a CI check, a deploy, a review, or another "
                        "agent, and will act or report once that changes?",
        "criteria": {
            "true": "It says it will report, continue, or check back when a job, build, reply, "
                    "or other agent's work finishes.",
            "false": "It finished, asked the user something, or stopped without mentioning "
                     "anything outside it that it waits for.",
        },
    },
    "nudge_again": {
        "type": "noul",
        "instructions": PREAMBLE + "Would sending another agent-nudge now plausibly cause useful "
                        "work or a useful blocker report? Use the trusted runtime continuity facts "
                        "above the terminal transcript as well as the visible conversation. This is "
                        "about whether asking again helps, not merely whether the session is idle.",
        "criteria": {
            "true": "The agent stopped short of a stated goal, left an actionable next step, gave an "
                    "unclear status, or said it was waiting without arranging a way to wake. A prior "
                    "nudge may have led to work that again stopped unfinished.",
            "false": "The agent explicitly says the goal is complete with nothing pending or blocked, "
                     "especially when runtime facts show an uninterrupted chain of nudge/reply cycles "
                     "and the transcript shows terminal answers rather than useful intervening work. "
                     "Also false when another identical prompt would only elicit the same answer.",
        },
    },
}


def enabled() -> bool:
    return bool(os.environ.get(KEY_ENV, "").strip())


def ask(tail: str, *, context: str = "", timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    state = f"{context.strip()}\n\nVisible terminal transcript:\n{tail}" if context.strip() else tail
    body = json.dumps({"state": state, "model": "jev-latest", "questions": QUESTIONS}).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ.get(KEY_ENV, '').strip()}",
                 "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    answers = data["answers"]
    return {
        "state": answers["state"]["choice"],
        "state_conf": answers["state"].get("confidence"),
        "needs_human_p": answers["needs_human"]["noul"],
        "waiting_p": answers["waiting"]["noul"],
        "nudge_again_p": answers["nudge_again"]["noul"],
        "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
