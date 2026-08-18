#!/usr/bin/env python3
"""Check the frame layout documented in PROTOCOL.md against real captures.

decode.py *discovers* structure; this asserts it. Once a field is understood, this
is what stops it silently regressing — when a new capture session disagrees with
the documented layout, that is either a damaged capture or a real gap in the model,
and both are worth hearing about immediately rather than after the component is
built on top of it.

It also cross-checks the frames against their labels, so it catches the label
mistakes that are easy to make during a capture run: a mislabelled temperature or
a power frame labelled as the wrong mode shows up here rather than quietly
corrupting the attribution of some other field.

Usage:
    python3 tools/verify_protocol.py                    # captures/
    python3 tools/verify_protocol.py captures/session-01.txt
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_raw import Capture, expand_paths, parse_files  # noqa: E402

EXPECTED_BITS = 112
# Nominal spaces; only used to classify each space as the near or far one, so the
# exact values matter far less than that they are well separated.
SPACE_ONE = 1060
SPACE_ZERO = 280

CONSTANT_PREFIX = [0x23, 0xCB, 0x26, 0x01, 0x00]
CONSTANT_TAIL = [0x00, 0x00, 0x00, 0x00]
POWER_ON, POWER_OFF = 0x24, 0x20
MODES: Dict[int, str] = {0x03: "cool", 0x02: "dry", 0x07: "fan"}
FAN_HIGH = 0x05
KNOWN_FAN = {FAN_HIGH, 0x02}
TEMP_OFFSET = 31  # byte 7 encodes TEMP_OFFSET - celsius


def frame_bytes(capture: Capture) -> List[int]:
    """Decode one capture into its 14 bytes, each read least significant bit first."""
    body = capture.timings[2:]  # drop the header mark/space pair
    bits = []
    for i in range(0, len(body) - 1, 2):
        space = abs(body[i + 1])
        bits.append("1" if abs(space - SPACE_ONE) < abs(space - SPACE_ZERO) else "0")
    bit_string = "".join(bits)
    if len(bit_string) != EXPECTED_BITS:
        raise ValueError(f"{len(bit_string)} bits, expected {EXPECTED_BITS}")
    return [int(bit_string[i : i + 8][::-1], 2) for i in range(0, EXPECTED_BITS, 8)]


def check(capture: Capture) -> List[str]:
    try:
        by = frame_bytes(capture)
    except ValueError as exc:
        return [str(exc)]

    problems: List[str] = []

    if by[:5] != CONSTANT_PREFIX:
        problems.append(f"prefix is {by[:5]}, expected {CONSTANT_PREFIX}")
    if by[9:13] != CONSTANT_TAIL:
        problems.append(f"bytes 9-12 are {by[9:13]}, expected zero")
    expected_sum = sum(by[:13]) & 0xFF
    if by[13] != expected_sum:
        problems.append(f"checksum {by[13]:#04x}, expected {expected_sum:#04x}")
    if by[5] not in (POWER_ON, POWER_OFF):
        problems.append(f"byte 5 (power) is {by[5]:#04x}")
    if by[6] not in MODES:
        problems.append(f"byte 6 (mode) is {by[6]:#04x}, not a known mode")
    if by[8] not in KNOWN_FAN:
        problems.append(f"byte 8 (fan) is {by[8]:#04x}, not a known value")

    # Cross-check against the label, where one exists.
    meta = capture.meta
    powered = by[5] == POWER_ON
    if "mode" in meta:
        if (meta["mode"] == "off") == powered:
            problems.append(
                f"byte 5 says {'on' if powered else 'off'}, label says mode={meta['mode']}"
            )
        if meta["mode"] != "off" and by[6] in MODES and MODES[by[6]] != meta["mode"]:
            problems.append(f"byte 6 says {MODES[by[6]]}, label says {meta['mode']}")
    if "temp" in meta:
        try:
            labelled = int(meta["temp"])
        except ValueError:
            problems.append(f"unparseable temp={meta['temp']}")
        else:
            if TEMP_OFFSET - by[7] != labelled:
                problems.append(
                    f"byte 7 decodes to {TEMP_OFFSET - by[7]}C, label says {labelled}C"
                )
    if meta.get("fan") == "high" and by[8] != FAN_HIGH:
        problems.append(f"byte 8 is {by[8]:#04x} on a fan=high capture")

    return problems


def main(argv: List[str]) -> int:
    patterns = argv[1:] or ["captures"]
    paths = expand_paths(patterns)
    if not paths:
        print("no capture files found", file=sys.stderr)
        return 1

    captures = parse_files(paths)
    if not captures:
        print("no captures parsed; is the logger at DEBUG and dump set to raw?", file=sys.stderr)
        return 1

    failures = 0
    for capture in captures:
        problems = check(capture)
        if problems:
            failures += 1
            print(f"[{capture.index:3d}] {capture.name}")
            for problem in problems:
                print(f"        {problem}")

    print(f"\nchecked {len(captures)} capture(s) against PROTOCOL.md")
    if failures:
        print(f"{failures} capture(s) disagree with the documented layout")
        return 1
    print("all consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
