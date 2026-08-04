from __future__ import annotations

import json
from pathlib import Path

from .model import JsonRecord, ReadWindow


def _decode_records(data: bytes, start: int) -> ReadWindow:
    records: list[JsonRecord] = []
    cursor = start
    lines = data.splitlines(keepends=True)
    partial = b""
    partial_start = start + len(data)

    for line in lines:
        end = cursor + len(line)
        if not line.endswith((b"\n", b"\r")):
            partial = line
            partial_start = cursor
            break
        raw = line.rstrip(b"\r\n")
        if raw:
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("record is not a JSON object")
                records.append(JsonRecord(cursor, end, value))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                records.append(JsonRecord(cursor, end, None, str(exc)))
        cursor = end

    return ReadWindow(tuple(records), partial, partial_start)


def read_window(path: Path, start: int, end: int) -> ReadWindow:
    """Read complete JSONL records in [start, end], discarding a split prefix."""
    if start < 0 or end < start:
        raise ValueError("invalid byte window")
    skipped_prefix = False
    with path.open("rb") as handle:
        if start:
            handle.seek(start - 1)
            previous = handle.read(1)
        else:
            previous = b"\n"
        handle.seek(start)
        data = handle.read(end - start)

    actual_start = start
    if start and previous not in (b"\n", b"\r"):
        newline = data.find(b"\n")
        skipped_prefix = True
        if newline < 0:
            return ReadWindow((), b"", end, skipped_prefix=True)
        actual_start = start + newline + 1
        data = data[newline + 1 :]

    result = _decode_records(data, actual_start)
    return ReadWindow(
        result.records,
        result.partial,
        result.partial_start,
        skipped_prefix=skipped_prefix,
    )


def read_append(
    path: Path,
    offset: int,
    end: int,
    partial: bytes = b"",
    partial_start: int | None = None,
) -> ReadWindow:
    """Read bytes appended after offset, completing a prior trailing record."""
    if end < offset:
        raise ValueError("append end precedes checkpoint")
    with path.open("rb") as handle:
        handle.seek(offset)
        appended = handle.read(end - offset)
    start = partial_start if partial and partial_start is not None else offset
    return _decode_records(partial + appended, start)
