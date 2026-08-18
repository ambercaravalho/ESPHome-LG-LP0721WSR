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
CONSTANT_TAIL = [0x00, 0x00, 0x00]  # bytes 10-12; byte 9 is the timer
POWER_BASE = 0x20
POWER_BIT = 0x04
TIMER_BIT = 0x08  # byte 5, set while a timer is armed
LIGHT_BIT = 0x40  # byte 5, marks a Light press; the frame carries no light value
TIMER_FLAG = 0x40  # byte 8, purpose unexplained; see PROTOCOL.md
TIMER_UNITS_PER_HOUR = 6  # byte 9 counts ten-minute units
TIMER_MAX = 24 * TIMER_UNITS_PER_HOUR
MODES: Dict[int, str] = {0x03: "cool", 0x02: "dry", 0x07: "fan"}
# Byte 8 is packed: fan in 0x07, swing in 0x38, the remote's timer mode in 0x40.
FAN_MASK = 0x07
SWING_MASK = 0x38
SWING_SHIFT = 3
SWING_ON = 0x07  # the field's all-ones value; 0 is off
FAN_HIGH = 0x05
KNOWN_FAN = {FAN_HIGH, 0x02}
TEMP_OFFSET = 31  # byte 7 encodes TEMP_OFFSET - celsius
# Below this a capture is ambient noise rather than a mangled frame. Well under
# the 112 real bits, so a truncated frame still gets reported.
NOISE_BITS = 16


def frame_bytes(capture: Capture) -> List[int]:
    """Decode one capture into its 14 bytes, each read least significant bit first."""
    body = capture.timings[2:]  # drop the header mark/space pair
    bits = []
    for i in range(0, len(body) - 1, 2):
        space = abs(body[i + 1])
        bits.append("1" if abs(space - SPACE_ONE) < abs(space - SPACE_ZERO) else "0")
    bit_string = "".join(bits)
    # Ambient IR -- sunlight, a TV remote in the next room, a reflection off the
    # AC's own front panel -- trips the receiver for a handful of symbols. Those
    # are not damaged frames and flagging them would leave the verifier
    # permanently red, so they are separated from a real length mismatch, which
    # means a genuine frame arrived truncated or merged.
    if len(bit_string) < NOISE_BITS:
        raise NoiseBurst(f"{len(bit_string)} bits, treated as noise")
    if len(bit_string) != EXPECTED_BITS:
        raise ValueError(f"{len(bit_string)} bits, expected {EXPECTED_BITS}")
    return [int(bit_string[i : i + 8][::-1], 2) for i in range(0, EXPECTED_BITS, 8)]


class NoiseBurst(ValueError):
    """A capture far too short to be a frame, e.g. a stray reflection or sunlight."""


def check(capture: Capture) -> List[str]:
    try:
        by = frame_bytes(capture)
    except NoiseBurst:
        return []
    except ValueError as exc:
        return [str(exc)]

    problems: List[str] = []

    if by[:5] != CONSTANT_PREFIX:
        problems.append(f"prefix is {by[:5]}, expected {CONSTANT_PREFIX}")
    if by[10:13] != CONSTANT_TAIL:
        problems.append(f"bytes 10-12 are {by[10:13]}, expected zero")
    expected_sum = sum(by[:13]) & 0xFF
    if by[13] != expected_sum:
        problems.append(f"checksum {by[13]:#04x}, expected {expected_sum:#04x}")
    if by[5] & ~(POWER_BIT | TIMER_BIT | LIGHT_BIT) != POWER_BASE:
        problems.append(f"byte 5 is {by[5]:#04x}, outside base|power|timer|light")
    if by[6] not in MODES:
        problems.append(f"byte 6 (mode) is {by[6]:#04x}, not a known mode")
    if by[8] & FAN_MASK not in KNOWN_FAN:
        problems.append(f"byte 8 fan bits are {by[8] & FAN_MASK:#x}, not a known speed")
    swing = (by[8] & SWING_MASK) >> SWING_SHIFT
    if swing not in (0, SWING_ON):
        problems.append(f"byte 8 swing field is {swing:#x}, expected 0 or {SWING_ON:#x}")
    if by[8] & ~(FAN_MASK | SWING_MASK | TIMER_FLAG):
        problems.append(f"byte 8 is {by[8]:#04x}, which sets an undocumented bit")

    # The timer is armed and disarmed by byte 5's flag, and byte 9 has to agree:
    # a nonzero value with the flag clear (or the reverse) would mean the two are
    # independent after all, which would invalidate the documented encoding.
    armed = bool(by[5] & TIMER_BIT)
    if armed != (by[9] != 0):
        problems.append(
            f"byte 5 timer bit is {'set' if armed else 'clear'} but byte 9 is {by[9]:#04x}"
        )
    if by[9] % TIMER_UNITS_PER_HOUR or by[9] > TIMER_MAX:
        problems.append(f"byte 9 is {by[9]:#04x}, not a whole hour in 1-24")

    # Cross-check against the label, where one exists.
    meta = capture.meta
    powered = bool(by[5] & POWER_BIT)
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
    if meta.get("fan") == "high" and by[8] & FAN_MASK != FAN_HIGH:
        problems.append(f"byte 8 is {by[8]:#04x} on a fan=high capture")
    if "swing" in meta:
        want = SWING_ON if meta["swing"] == "on" else 0
        if swing != want:
            problems.append(f"byte 8 swing field is {swing:#x}, label says swing={meta['swing']}")

    # Light is the one button whose bit we can check in both directions, since the
    # frame is otherwise an ordinary state frame. A mismatch either way means the
    # bit is not the Light press after all.
    lit = bool(by[5] & LIGHT_BIT)
    if (meta.get("button") == "light") != lit:
        problems.append(
            f"byte 5 light bit is {'set' if lit else 'clear'}, "
            f"label says button={meta.get('button', 'none')}"
        )

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
    noise = 0
    for capture in captures:
        try:
            frame_bytes(capture)
        except NoiseBurst:
            noise += 1
            continue
        except ValueError:
            pass
        problems = check(capture)
        if problems:
            failures += 1
            print(f"[{capture.index:3d}] {capture.name}")
            for problem in problems:
                print(f"        {problem}")

    print(f"\nchecked {len(captures) - noise} capture(s) against PROTOCOL.md")
    if noise:
        print(f"ignored {noise} ambient noise burst(s) too short to be a frame")
    if failures:
        print(f"{failures} capture(s) disagree with the documented layout")
        return 1
    print("all consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
