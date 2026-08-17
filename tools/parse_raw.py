#!/usr/bin/env python3
"""Extract raw IR captures from ESPHome ``remote_receiver`` log output.

ESPHome's raw dumper emits one capture as a ``Received Raw:`` line followed by
an arbitrary number of continuation lines, all tagged ``remote.raw``::

    [13:59:14][D][remote.raw:028]: Received Raw: 3347, -9895, 400, -1629,
    [13:59:14][D][remote.raw:041]:   425, -589, 451, -631, 401, -1645, 427

Positive numbers are marks (carrier on), negative are spaces, in microseconds.

Captures are labelled by the ``# state: key=value ...`` markers that the
capture firmware writes into the log when you press "Mark Capture". A marker
applies to every capture that follows it until the next marker.

Also accepts hand-pasted files containing bare comma-separated numbers or JSON
arrays, one capture per line, with ``#`` comment lines for the markers.

Usage:
    python3 tools/parse_raw.py captures/*.txt
    python3 tools/parse_raw.py captures/*.txt --json > frames.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

# Refuse absurdly large inputs rather than exhausting memory on a bad path.
MAX_INPUT_BYTES = 32 * 1024 * 1024

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# ESPHome writes "[time][level][tag:line]: body", so the colon that separates
# the bracketed prefix from the body has to be consumed here or continuation
# lines stop looking like bare number lists.
PREFIX_RE = re.compile(r"^((?:\[[^\]]*\])*):?\s*(.*)$")
BRACKET_RE = re.compile(r"\[([^\]]*)\]")
TAG_RE = re.compile(r"^([A-Za-z0-9_.]+):(\d+)$")
NUMBER_RE = re.compile(r"-?\d+")
NUMBERS_ONLY_RE = re.compile(r"^[\s\d,+\-\[\]]+$")
STATE_RE = re.compile(r"#\s*state:\s*(.*?)\s*$")
RECEIVED_RAW_RE = re.compile(r"Received Raw:\s*(.*)$")

RAW_TAG = "remote.raw"


@dataclass
class Capture:
    """One raw IR frame plus whatever we know about the state it represents."""

    index: int
    timings: List[int]
    label: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    source: str = "<stdin>"

    @property
    def name(self) -> str:
        return self.label or f"capture-{self.index}"

    @property
    def marks(self) -> List[int]:
        return [v for v in self.timings if v > 0]

    @property
    def spaces(self) -> List[int]:
        return [-v for v in self.timings if v < 0]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "meta": self.meta,
            "source": self.source,
            "symbols": len(self.timings),
            "timings": self.timings,
        }


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _split_prefix(line: str):
    """Split an ESPHome log line into (tag, body).

    Returns ``(None, line)`` for lines that carry no ``[tag:line]`` prefix.
    """
    match = PREFIX_RE.match(line)
    if not match:
        return None, line
    prefix, body = match.group(1), match.group(2)
    tag = None
    for bracket in BRACKET_RE.findall(prefix):
        tag_match = TAG_RE.match(bracket)
        if tag_match:
            tag = tag_match.group(1)
    return tag, body


def parse_meta(label: str) -> Dict[str, str]:
    """Turn ``mode=cool temp=24 fan=high`` into a dict."""
    meta: Dict[str, str] = {}
    for token in label.replace(",", " ").split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip().lower()
        if key and value:
            meta[key] = value
    return meta


def parse_numbers(text: str) -> List[int]:
    return [int(n) for n in NUMBER_RE.findall(text)]


def parse_text(text: str, source: str = "<stdin>", start_index: int = 0) -> List[Capture]:
    """Pull every raw capture out of one log or paste."""
    captures: List[Capture] = []
    pending_label = ""
    current: Optional[Capture] = None
    index = start_index

    def finish() -> None:
        nonlocal current
        if current is not None and current.timings:
            captures.append(current)
        current = None

    for line in strip_ansi(text).splitlines():
        tag, body = _split_prefix(line.rstrip())

        state_match = STATE_RE.search(body)
        if state_match:
            finish()
            pending_label = state_match.group(1)
            continue

        raw_match = RECEIVED_RAW_RE.search(body)
        if raw_match:
            finish()
            current = Capture(
                index=index,
                timings=parse_numbers(raw_match.group(1)),
                label=pending_label,
                meta=parse_meta(pending_label),
                source=source,
            )
            index += 1
            continue

        # Continuation of the capture in progress. ESPHome wraps long dumps
        # across several lines that carry the same tag and nothing but numbers.
        if current is not None and tag == RAW_TAG and body and NUMBERS_ONLY_RE.match(body):
            current.timings.extend(parse_numbers(body))
            continue

        # A differently-tagged line (another dumper, a state log, anything)
        # terminates the capture.
        if current is not None:
            finish()

        # Fallback for hand-pasted arrays: a bare, untagged list of numbers
        # where at least one is negative, which no other log line looks like.
        if tag is None and body and NUMBERS_ONLY_RE.match(body):
            values = parse_numbers(body)
            if len(values) >= 8 and any(v < 0 for v in values):
                captures.append(
                    Capture(
                        index=index,
                        timings=values,
                        label=pending_label,
                        meta=parse_meta(pending_label),
                        source=source,
                    )
                )
                index += 1

    finish()
    return captures


def parse_files(paths: Sequence[Path]) -> List[Capture]:
    captures: List[Capture] = []
    for path in paths:
        # SECURITY-REVIEW: file system access with user-supplied paths. This is
        # an offline developer tool run by the repo owner against files in the
        # repo, so the size cap below is the only guard we need.
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise SystemExit(f"{path}: refusing to read more than {MAX_INPUT_BYTES} bytes")
        text = path.read_text(encoding="utf-8", errors="replace")
        captures.extend(parse_text(text, source=str(path), start_index=len(captures)))
    return captures


def expand_paths(patterns: Iterable[str]) -> List[Path]:
    """Resolve arguments, expanding any globs the shell left alone."""
    paths: List[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.txt")))
        elif candidate.exists():
            paths.append(candidate)
        else:
            matches = sorted(Path().glob(pattern))
            if not matches:
                raise SystemExit(f"no such file: {pattern}")
            paths.extend(matches)
    # README.md lives alongside the captures; skip anything non-text.
    return [p for p in paths if p.suffix.lower() in {".txt", ".log", ".json"}]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="log files, or directories of them")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a summary")
    args = parser.parse_args(argv)

    captures = parse_files(expand_paths(args.paths))
    if not captures:
        print("No raw captures found. Is the logger at DEBUG and dump enabled?", file=sys.stderr)
        return 1

    if args.json:
        json.dump([c.to_dict() for c in captures], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"{len(captures)} capture(s)\n")
    for capture in captures:
        unlabelled = "" if capture.label else "  (unlabelled)"
        print(f"[{capture.index:3d}] {len(capture.timings):4d} symbols  {capture.name}{unlabelled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
