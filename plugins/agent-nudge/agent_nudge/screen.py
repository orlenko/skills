"""Read an agent's tmux screen: where the input box is, whether it holds typed
text, what changed above it, and what the footer says is running.

Claude Code draws its prompt as `❯`, Codex as `›`. Both draw suggestions and
placeholders in dim text, so a line reading `❯ check messages` can be empty.
Only text without the dim attribute counts as typed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

PROMPT_GLYPHS = {"claude": "❯", "codex": "›"}
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC = re.compile(r"\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
_RULE = re.compile(r"^[\s─━═╌┄-]+(\S.*\S\s*[─━═]+\s*)?$")
# Claude's footer counts what keeps running after a turn: "· 1 monitor ·",
# "2 background tasks", "1 shell". Any of them can wake the session.
# A narrow pane cuts the footer mid-word ("· 1 monit"), and the finished-turn
# line may say it instead ("done 6:33 PM · 1 monitor still running"), so both
# are read and a truncated word still counts.
_WATCHERS = re.compile(r"\b(\d+)\s+(monit\w*|background\s+tasks?|shells?|bash\w*|jobs?)", re.I)
_WORKING = re.compile(r"esc to interrupt|ctrl\+c to interrupt|press esc to stop", re.I)
# Claude's live spinner: a glyph and one word ending in an ellipsis ("✳ Nucleating…").
# The finished line reads "✻ Cogitated for 13s · done 5:42 PM", with no ellipsis.
_SPINNER = re.compile(r"^\s*\S\s+[A-Z][\w-]*…")


# A reply that stops at an obstacle: "blocked on", "waiting for X", "until #73
# merges", "asked the conductor for a ruling", "if you'd rather I just go". A
# wait on the user ("waiting on your recording") is a person's call and doesn't
# count. Jev couldn't tell these apart on real screens (good and bad cases both
# scored 0.3-0.75), so this is a phrase match: a wrong push-back costs one line.
_BLOCKER = re.compile(
    r"\bblocked\b|\bblock(?:s|ing) on\b"
    r"|\bwaiting (?:on|for) (?!(?:you|your|the user|vlad)\b)"
    r"|\b(?:until|once|when)\b[^.\n]{0,80}\b(?:merges?|merged|lands?|landed|lifts?|clears?)\b"
    r"|\bgates? (?:are|is) (?:still )?shut\b|\bnothing (?:else )?for me to do\b"
    r"|\b(?:asked|asking|await(?:ing)?)\b[^.\n]{0,60}\b(?:ruling|go-ahead|permission|sign-off)\b"
    r"|\bif you'?d rather I\b|\bsay so and I'?ll\b",
    re.I,
)


def reply_to_nudge(body: str, sent: str = "") -> str | None:
    """What the agent wrote after the last nudge on screen, or None if no nudge is visible.

    The nudge wraps over several screen lines, so the text `sent` is skipped
    character by character, ignoring the whitespace wrapping put in.
    """
    start = body.rfind("[agent-nudge]")
    if start < 0:
        return None
    rest = body[start:]
    if sent:
        wanted = [c for c in sent if not c.isspace()]
        i = matched = 0
        while i < len(rest) and matched < len(wanted):
            if rest[i].isspace():
                i += 1
            elif rest[i] == wanted[matched]:
                i += 1
                matched += 1
            else:
                break
        rest = rest[i:]
    else:
        rest = rest.split("\n", 1)[1] if "\n" in rest else ""
    reply = rest.strip()
    return reply or None


_NEGATED_BEFORE = re.compile(r"\b(?:nothing|not|no|no longer|isn't|aren't|never|without)\b[^.;:\n]{0,40}$", re.I)
_NEGATED_AFTER = re.compile(r"^[^.;:\n]{0,15}\b(?:and|but) (?:isn't|is not|aren't)\b", re.I)


def claims_blocker(text: str) -> bool:
    """A stated obstacle, not a denial of one ("nothing is blocked", "looks blocked and isn't")."""
    flat = " ".join(text.split())
    for match in _BLOCKER.finditer(flat):
        before, after = flat[max(0, match.start() - 60):match.start()], flat[match.end():match.end() + 30]
        if _NEGATED_BEFORE.search(before) or _NEGATED_AFTER.search(after):
            continue
        return True
    return False


@dataclass
class Screen:
    has_prompt: bool
    typed: str
    body: str
    body_hash: str
    tail: str
    footer: str
    watchers: int
    working_marker: bool


def _plain_and_dim(line: str) -> tuple[str, list[bool]]:
    line = _OTHER_ESC.sub("", line)
    text, dim_mask, dim, pos = [], [], False, 0
    for match in _SGR.finditer(line):
        chunk = line[pos:match.start()]
        text.append(chunk)
        dim_mask.extend([dim] * len(chunk))
        codes = [c for c in match.group(1).split(";") if c != ""] or ["0"]
        for code in codes:
            if code in {"0", "22"}:
                dim = False
            elif code == "2":
                dim = True
        pos = match.end()
    chunk = line[pos:]
    text.append(chunk)
    dim_mask.extend([dim] * len(chunk))
    return "".join(text), dim_mask


def _count_watchers(text: str) -> int:
    return sum(int(m.group(1)) for m in _WATCHERS.finditer(text))


def parse(raw: str, agent: str, tail_lines: int = 40) -> Screen:
    glyph = PROMPT_GLYPHS.get(agent, "❯")
    lines = raw.rstrip("\n").split("\n")
    parsed = [_plain_and_dim(line) for line in lines]
    prompt_at = None
    for index in range(len(parsed) - 1, -1, -1):
        text = parsed[index][0].lstrip(" │┃")
        if text.startswith(glyph):
            prompt_at = index
            break
    if prompt_at is None:
        body_lines = [text for text, _ in parsed]
        footer, typed = "", ""
    else:
        text, dim = parsed[prompt_at]
        start = text.index(glyph) + len(glyph)
        typed = "".join(ch for ch, d in zip(text[start:], dim[start:]) if not d)
        typed = typed.replace("\xa0", " ").strip(" │┃")
        body_lines = [t for t, _ in parsed[:prompt_at]]
        footer = "\n".join(t for t, _ in parsed[prompt_at + 1:])
    # The input box's top rule and trailing blanks change with width, not work.
    while body_lines and (not body_lines[-1].strip() or _RULE.match(body_lines[-1])):
        body_lines.pop()
    body = "\n".join(line.rstrip() for line in body_lines)
    tail = "\n".join(line.rstrip() for line in body_lines[-tail_lines:] if line.strip())
    return Screen(
        has_prompt=prompt_at is not None,
        typed=typed.strip(),
        body=body,
        body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        tail=tail,
        footer=footer,
        watchers=max(_count_watchers(footer), _count_watchers(" ".join(body_lines[-3:]))),
        working_marker=bool(_WORKING.search(tail[-600:] + "\n" + footer))
        or any(_SPINNER.match(line) for line in body_lines[-4:]),
    )
