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
