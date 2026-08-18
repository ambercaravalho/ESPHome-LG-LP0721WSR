#!/usr/bin/env python3
"""Reverse-engineer an AC remote's IR frame format from raw ESPHome captures.

Given a directory of logs produced by ``firmware/xiao-ir-capture.yaml``, this:

1. Infers the pulse timings (header, bit mark, zero/one spaces, mid-frame gaps)
   by clustering every duration in the capture set.
2. Decodes each frame to bits and shows it as bytes in both MSB-first and
   LSB-first orderings.
3. Checks that repeated captures of the same state are identical, which tells us
   whether the protocol is stateful or carries a toggle/sequence bit.
4. Attributes bit positions to ``mode`` / ``temp`` / ``fan`` / ``timer`` by
   diffing every pair of captures that differs in exactly one labelled key.
5. Brute-forces the checksum: sum/xor over nibbles or bytes, over several
   payload ranges, with a constant offset.
6. Summarises the frame byte by byte, naming which label moves each byte.

Usage:
    python3 research/tools/decode.py research/captures/
    python3 research/tools/decode.py research/captures/*.txt --layout
    python3 research/tools/decode.py research/captures/ --bit-order lsb -v
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from functools import reduce
from itertools import combinations
from operator import xor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_raw import Capture, expand_paths, parse_files  # noqa: E402

# Durations within this ratio of each other are treated as the same symbol.
CLUSTER_RATIO = 1.6
# A space at least this many times the "zero" space is a mid-frame gap, not a bit.
GAP_FACTOR = 2.5
# Keys that describe climate state rather than which button was pressed.
STATE_KEYS = ("mode", "temp", "fan", "swing", "light", "timer")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    values: List[int]

    @property
    def median(self) -> int:
        return int(statistics.median(self.values))

    @property
    def count(self) -> int:
        return len(self.values)

    def __str__(self) -> str:
        return f"{self.median:6d} us  (n={self.count:4d}, {min(self.values)}..{max(self.values)})"


def cluster(values: Sequence[int], ratio: float = CLUSTER_RATIO) -> List[Cluster]:
    """Split durations into groups, breaking wherever there is a relative jump."""
    positives = sorted(v for v in values if v > 0)
    if not positives:
        return []
    groups: List[List[int]] = [[positives[0]]]
    for value in positives[1:]:
        if value > groups[-1][-1] * ratio:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [Cluster(g) for g in groups]


# ---------------------------------------------------------------------------
# Timing model
# ---------------------------------------------------------------------------


@dataclass
class Timing:
    header_mark: int
    header_space: int
    bit_mark: int
    zero_space: int
    one_space: int
    gap_threshold: int
    gap_space: Optional[int] = None
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"  header mark   : {self.header_mark:6d} us",
            f"  header space  : {self.header_space:6d} us",
            f"  bit mark      : {self.bit_mark:6d} us",
            f"  zero space    : {self.zero_space:6d} us",
            f"  one space     : {self.one_space:6d} us",
        ]
        if self.gap_space:
            lines.append(f"  mid-frame gap : {self.gap_space:6d} us")
        return "\n".join(lines)


def infer_timing(captures: Sequence[Capture]) -> Timing:
    header_marks: List[int] = []
    header_spaces: List[int] = []
    data_marks: List[int] = []
    data_spaces: List[int] = []

    for capture in captures:
        values = capture.timings
        if len(values) < 6 or values[0] <= 0 or values[1] >= 0:
            continue
        header_marks.append(values[0])
        header_spaces.append(-values[1])
        for value in values[2:]:
            (data_marks if value > 0 else data_spaces).append(abs(value))

    if not header_marks or not data_spaces:
        raise SystemExit("Not enough usable captures to infer a timing model.")

    warnings: List[str] = []

    mark_clusters = cluster(data_marks)
    mark_clusters.sort(key=lambda c: c.count, reverse=True)
    bit_mark = mark_clusters[0].median
    significant_marks = [c for c in mark_clusters if c.count >= 0.1 * len(data_marks)]
    if len(significant_marks) > 1:
        warnings.append(
            "Two or more distinct mark widths carry real weight. This protocol may "
            "encode bits in the mark rather than the space; the bit values below "
            "are probably wrong. Inspect the per-frame dump."
        )

    space_clusters = sorted(cluster(data_spaces), key=lambda c: c.median)
    zero_space = space_clusters[0].median
    one_space: Optional[int] = None
    gap_space: Optional[int] = None
    for candidate in space_clusters[1:]:
        median = candidate.median
        if one_space is None and 1.7 * zero_space <= median <= 6 * zero_space:
            one_space = median
        elif one_space is not None and median > GAP_FACTOR * one_space:
            gap_space = median

    if one_space is None:
        raise SystemExit(
            "Could not separate zero-spaces from one-spaces. Every data space "
            f"clustered around {zero_space}us. Capture more states so the frames "
            "actually differ, or lower the receiver's `filter`."
        )

    gap_threshold = int(one_space * GAP_FACTOR)

    return Timing(
        header_mark=int(statistics.median(header_marks)),
        header_space=int(statistics.median(header_spaces)),
        bit_mark=bit_mark,
        zero_space=zero_space,
        one_space=one_space,
        gap_threshold=gap_threshold,
        gap_space=gap_space,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Bit decoding
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    capture: Capture
    bits: List[int]
    chunk_boundaries: List[int]
    trailer_mark: Optional[int]
    errors: List[str]

    @property
    def bit_string(self) -> str:
        return "".join(str(b) for b in self.bits)

    @property
    def byte_aligned(self) -> bool:
        return len(self.bits) % 8 == 0

    @property
    def nibble_hex(self) -> str:
        """The frame as hex nibbles, first transmitted bit most significant.

        This is how ESPHome logs LG codes (`0x880C646`) and the only honest
        view of a frame whose length is not a multiple of 8.
        """
        nibbles = frame_nibbles(self.bits, lsb_first=False) or []
        text = "".join(f"{n:X}" for n in nibbles)
        remainder = len(self.bits) % 4
        return text + ("?" * remainder if remainder else "")

    def bytes_view(self, lsb_first: bool) -> Optional[bytes]:
        if not self.byte_aligned:
            return None
        return bits_to_bytes(self.bits, lsb_first)

    def hex_view(self, lsb_first: bool) -> str:
        data = self.bytes_view(lsb_first)
        if data is None:
            return f"(not byte-aligned: {len(self.bits)} bits)"
        return " ".join(f"{b:02X}" for b in data)


def decode_bits(capture: Capture, timing: Timing) -> Frame:
    values = capture.timings
    bits: List[int] = []
    chunk_boundaries: List[int] = []
    errors: List[str] = []
    trailer: Optional[int] = None

    index = 2
    while index < len(values):
        mark = values[index]
        if mark <= 0:
            errors.append(f"expected a mark at symbol {index}, got {mark}")
            break
        if index + 1 >= len(values):
            trailer = mark
            break
        space = -values[index + 1]
        if space <= 0:
            errors.append(f"expected a space at symbol {index + 1}, got {values[index + 1]}")
            break
        if space >= timing.gap_threshold:
            chunk_boundaries.append(len(bits))
            index += 2
            continue
        one_distance = abs(space - timing.one_space)
        zero_distance = abs(space - timing.zero_space)
        bits.append(1 if one_distance < zero_distance else 0)
        index += 2

    return Frame(capture, bits, chunk_boundaries, trailer, errors)


def bits_to_bytes(bits: Sequence[int], lsb_first: bool) -> bytes:
    out = bytearray()
    for start in range(0, len(bits), 8):
        chunk = list(bits[start : start + 8])
        chunk += [0] * (8 - len(chunk))
        if lsb_first:
            out.append(sum(bit << i for i, bit in enumerate(chunk)))
        else:
            out.append(sum(bit << (7 - i) for i, bit in enumerate(chunk)))
    return bytes(out)


def to_nibbles(data: bytes) -> List[int]:
    nibbles: List[int] = []
    for byte in data:
        nibbles.append(byte >> 4)
        nibbles.append(byte & 0xF)
    return nibbles


def frame_nibbles(bits: Sequence[int], lsb_first: bool) -> Optional[List[int]]:
    """Nibbles of a frame, or None if the requested ordering is meaningless.

    "LSB-first" describes byte-oriented protocols that transmit each byte least
    significant bit first, so it only has meaning when the frame divides evenly
    into bytes. Zero-padding a 28-bit frame up to 32 bits to force the issue
    invents a nibble that is not in the signal and makes the checksum search
    chase it, so refuse instead.
    """
    if lsb_first:
        if len(bits) % 8:
            return None
        return to_nibbles(bits_to_bytes(bits, lsb_first=True))
    # MSB-first needs no byte alignment; drop any trailing partial nibble.
    return [bits_value(bits[start : start + 4]) for start in range(0, len(bits) - 3, 4)]


def bits_value(bits: Sequence[int]) -> int:
    """Interpret a bit slice as an integer, first bit most significant."""
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def check_determinism(frames: Sequence[Frame]) -> List[str]:
    groups: Dict[Tuple, List[Frame]] = {}
    for frame in frames:
        if not frame.capture.meta:
            continue
        key = tuple(sorted(frame.capture.meta.items()))
        groups.setdefault(key, []).append(frame)

    findings: List[str] = []
    for key, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        distinct = {frame.bit_string for frame in group}
        label = " ".join(f"{k}={v}" for k, v in key)
        if len(distinct) == 1:
            findings.append(f"  stable   ({len(group)}x)  {label}")
        else:
            findings.append(
                f"  VARIES   ({len(group)}x, {len(distinct)} distinct)  {label}"
            )
            for bit_string in sorted(distinct):
                findings.append(f"             {bit_string}")
    return findings


# ---------------------------------------------------------------------------
# Field attribution
# ---------------------------------------------------------------------------


def differing_keys(left: Dict[str, str], right: Dict[str, str], keys: Sequence[str]) -> List[str]:
    return [k for k in keys if left.get(k) != right.get(k)]


def enclosing_span(indices: Set[int]) -> Tuple[int, int]:
    """The smallest single run that covers every index."""
    return min(indices), max(indices)


def runs(indices: Set[int]) -> List[Tuple[int, int]]:
    """Collapse a set of bit indices into inclusive (start, end) runs."""
    out: List[Tuple[int, int]] = []
    for index in sorted(indices):
        if out and index == out[-1][1] + 1:
            out[-1] = (out[-1][0], index)
        else:
            out.append((index, index))
    return out


@dataclass
class Attribution:
    by_key: Dict[str, Set[int]]
    pair_counts: Dict[str, int]
    unexplained: Set[int]


def attribute_fields(frames: Sequence[Frame]) -> Attribution:
    labelled = [f for f in frames if f.capture.meta]
    by_key: Dict[str, Set[int]] = {}
    pair_counts: Dict[str, int] = {}
    explained: Set[int] = set()
    varying: Set[int] = set()

    widths = {len(f.bits) for f in labelled}
    reference_width = max(widths, default=0)

    for left, right in combinations(labelled, 2):
        if len(left.bits) != reference_width or len(right.bits) != reference_width:
            continue
        diff = {i for i in range(len(left.bits)) if left.bits[i] != right.bits[i]}
        if not diff:
            continue
        varying |= diff
        changed = differing_keys(left.capture.meta, right.capture.meta, STATE_KEYS)
        if len(changed) != 1:
            continue
        key = changed[0]
        by_key.setdefault(key, set()).update(diff)
        pair_counts[key] = pair_counts.get(key, 0) + 1
        explained |= diff

    return Attribution(by_key, pair_counts, varying - explained)


def field_values(frames: Sequence[Frame], key: str, span: Tuple[int, int]) -> List[Tuple[str, int]]:
    start, end = span
    seen: Dict[str, int] = {}
    for frame in frames:
        value = frame.capture.meta.get(key)
        if value is None or end >= len(frame.bits):
            continue
        seen.setdefault(value, bits_value(frame.bits[start : end + 1]))
    return sorted(seen.items(), key=lambda kv: (len(kv[0]), kv[0]))


# ---------------------------------------------------------------------------
# Checksum search
# ---------------------------------------------------------------------------

ALGORITHMS = {
    "nibble_sum": lambda nibbles: sum(nibbles),
    "nibble_xor": lambda nibbles: reduce(xor, nibbles, 0),
    "byte_sum": lambda nibbles: sum(
        (nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles) - 1, 2)
    ),
    "byte_xor": lambda nibbles: reduce(
        xor, [(nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles) - 1, 2)], 0
    ),
}


@dataclass
class ChecksumMatch:
    bit_order: str
    algorithm: str
    checksum_nibbles: int
    skip_nibbles: int
    sign: int
    offset: int

    def describe(self) -> str:
        width = self.checksum_nibbles * 4
        sign = "" if self.sign > 0 else "-"
        offset = f" + 0x{self.offset:X}" if self.offset else ""
        return (
            f"  {self.bit_order}-first: checksum = ({sign}{self.algorithm}"
            f"(nibbles[{self.skip_nibbles}:-{self.checksum_nibbles}]){offset}) "
            f"& 0x{(1 << width) - 1:X}   -> last {width} bits"
        )


def find_checksums(frames: Sequence[Frame], bit_order: str) -> List[ChecksumMatch]:
    lsb_first = bit_order == "lsb"
    samples: List[List[int]] = []
    for frame in frames:
        if len(frame.bits) < 16:
            continue
        nibbles = frame_nibbles(frame.bits, lsb_first)
        if nibbles:
            samples.append(nibbles)
    if len(samples) < 3:
        return []

    lengths = {len(s) for s in samples}
    if len(lengths) != 1:
        # Mixed frame lengths cannot share one checksum layout; use the majority.
        common = statistics.mode([len(s) for s in samples])
        samples = [s for s in samples if len(s) == common]
    if len(samples) < 3:
        return []

    total_nibbles = len(samples[0])
    matches: List[ChecksumMatch] = []

    for checksum_nibbles in (1, 2):
        mask = (1 << (checksum_nibbles * 4)) - 1
        observed = [bits_value_from_nibbles(s[-checksum_nibbles:]) for s in samples]
        # A constant checksum field is not a checksum.
        if len(set(observed)) < 2:
            continue
        for skip in range(0, min(5, total_nibbles - checksum_nibbles)):
            payloads = [s[skip : total_nibbles - checksum_nibbles] for s in samples]
            if not payloads[0]:
                continue
            for name, algorithm in ALGORITHMS.items():
                if name.startswith("byte") and len(payloads[0]) % 2:
                    continue
                raws = [algorithm(p) for p in payloads]
                for sign in (1, -1):
                    offsets = {(want - sign * raw) & mask for raw, want in zip(raws, observed)}
                    if len(offsets) == 1:
                        matches.append(
                            ChecksumMatch(
                                bit_order=bit_order,
                                algorithm=name,
                                checksum_nibbles=checksum_nibbles,
                                skip_nibbles=skip,
                                sign=sign,
                                offset=offsets.pop(),
                            )
                        )
    return matches


def collapse_checksums(
    matches: Sequence[ChecksumMatch],
) -> List[Tuple[ChecksumMatch, int]]:
    """Fold away candidates that differ only in where the sum starts.

    ``byte_sum(nibbles[2:]) + 0x23`` is not a rival theory to
    ``byte_sum(nibbles[0:])``; it is the same sum with a constant leading byte
    omitted and added straight back. Whenever the skipped nibbles belong to the
    frame's constant prefix the two agree on every valid frame, so no amount of
    further capture will separate them. Listing both invites a hunt for evidence
    that cannot exist.

    Keeps the form starting nearest nibble 0 for each distinct algorithm, and
    reports how many equivalent spellings collapsed into it.
    """
    best: Dict[Tuple, ChecksumMatch] = {}
    counts: Counter = Counter()
    for match in matches:
        key = (match.bit_order, match.algorithm, match.sign, match.checksum_nibbles)
        counts[key] += 1
        current = best.get(key)
        if current is None or match.skip_nibbles < current.skip_nibbles:
            best[key] = match
    return [(match, counts[key]) for key, match in best.items()]


def bits_value_from_nibbles(nibbles: Sequence[int]) -> int:
    value = 0
    for nibble in nibbles:
        value = (value << 4) | nibble
    return value


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def banner(title: str) -> str:
    return f"\n{'=' * 74}\n{title}\n{'=' * 74}"


def report(frames: Sequence[Frame], timing: Timing, bit_orders: Sequence[str], verbose: bool) -> Dict:
    reference_width = max((len(f.bits) for f in frames), default=0)

    print(banner("TIMING MODEL"))
    print(timing.describe())
    for warning in timing.warnings:
        print(f"\n  WARNING: {warning}")

    print(banner("FRAMES"))
    widths = sorted({len(f.bits) for f in frames})
    print(f"  {len(frames)} frame(s), bit length(s): {', '.join(str(w) for w in widths)}")
    if len(widths) > 1:
        print("  WARNING: frames have different bit lengths. Either some captures are")
        print("           truncated, or the protocol uses variable-length messages.")
    chunked = [f for f in frames if f.chunk_boundaries]
    if chunked:
        boundaries = sorted({tuple(f.chunk_boundaries) for f in chunked})
        print(f"  mid-frame gaps after bit index: {boundaries}")

    print()
    for frame in frames:
        label = frame.capture.name
        print(f"  [{frame.capture.index:3d}] {label}")
        print(f"        bits  {frame.bit_string}")
        print(f"        hex   0x{frame.nibble_hex}")
        if frame.byte_aligned:
            for order in bit_orders:
                print(f"        {order.upper():3s}   {frame.hex_view(order == 'lsb')}")
        if frame.errors:
            for error in frame.errors:
                print(f"        ERROR {error}")
        if verbose:
            print(f"        symbols={len(frame.capture.timings)} trailer={frame.trailer_mark}")

    print(banner("DETERMINISM"))
    findings = check_determinism(frames)
    if findings:
        print("\n".join(findings))
        if any("VARIES" in f for f in findings):
            print(
                "\n  At least one state produced different frames on different presses.\n"
                "  The protocol carries a toggle or sequence bit, so the component will\n"
                "  need to alternate it. See PROTOCOL.md."
            )
    else:
        print("  Not enough repeated states to judge. Capture section A of the matrix.")

    print(banner("CHECKSUM CANDIDATES"))
    all_matches: List[ChecksumMatch] = []
    for order in bit_orders:
        matches = collapse_checksums(find_checksums(frames, order))
        all_matches.extend(match for match, _ in matches)
        if matches:
            for match, forms in matches:
                suffix = (
                    f"   [{forms} equivalent forms, differing only in where the sum starts]"
                    if forms > 1
                    else ""
                )
                print(match.describe() + suffix)
        else:
            print(f"  {order}-first: no consistent checksum found")
    if len(all_matches) > 1:
        print("\n  More than one hypothesis fits. Capture more distinct states to")
        print("  eliminate the coincidences, then re-run.")
    elif len(all_matches) == 1:
        print("\n  Exactly one algorithm fits every captured frame.")

    # Every field change also changes the checksum, so knowing where the
    # checksum lives lets us report real fields instead of a field plus its
    # checksum echo. When hypotheses of different widths all fit, take the widest:
    # a narrower guess leaves the remaining checksum bits varying with every
    # single state change, which then shows up as a phantom run under every field.
    # Over-excluding is the safer error, because a checksum hypothesis has to hold
    # across every captured frame to be reported at all.
    checksum_bits: Set[int] = set()
    if all_matches:
        checksum_width = max(match.checksum_nibbles for match in all_matches) * 4
        checksum_bits = set(range(reference_width - checksum_width, reference_width))

    print(banner("FIELD ATTRIBUTION"))
    attribution = attribute_fields(frames)
    if not attribution.by_key:
        print("  Nothing attributable. This needs captures that differ in exactly one")
        print("  labelled key; see research/capture-guide.md for the matrix that guarantees it.")
    if checksum_bits:
        low, high = min(checksum_bits), max(checksum_bits)
        widths = sorted({match.checksum_nibbles * 4 for match in all_matches})
        note = f", widest of {widths} that fit" if len(widths) > 1 else ""
        print(f"  (bits {low}..{high} are the checksum{note}, and are excluded below)")
    for key in STATE_KEYS:
        indices = attribution.by_key.get(key)
        if not indices:
            continue
        pairs = attribution.pair_counts.get(key, 0)
        field_bits = indices - checksum_bits
        note = "" if indices <= field_bits else ", plus the checksum"
        print(f"\n  {key}  (from {pairs} single-variable pair(s){note})")
        if not field_bits:
            print("    only checksum bits moved; this key is probably not encoded")
            continue
        spans = runs(field_bits)
        for span in spans:
            start, end = span
            shift = reference_width - 1 - end
            print(
                f"    bits {start}..{end}  width {end - start + 1}"
                f"  -> frame integer shift {shift}"
            )
            for value, encoded in field_values(frames, key, span):
                print(f"       {key}={value:<6s} -> 0x{encoded:X} ({encoded})")
        if len(spans) > 1:
            span = enclosing_span(field_bits)
            start, end = span
            print(
                f"    enclosing span bits {start}..{end}  width {end - start + 1}"
                f"  -> frame integer shift {reference_width - 1 - end}"
            )
            print("      Several runs moved together, so the real field is probably the")
            print("      whole span with some bits constant across the states captured:")
            for value, encoded in field_values(frames, key, span):
                print(f"       {key}={value:<6s} -> 0x{encoded:X} ({encoded})")
    if attribution.unexplained - checksum_bits:
        remaining = sorted(attribution.unexplained - checksum_bits)
        print(f"\n  Unattributed but varying bits: {remaining}")
        print("  These change without any single label explaining them. Usually it means")
        print("  two captures differed in more than one label at once: an `off` capture")
        print("  that dropped its temp= and fan= labels is the common culprit. Re-label")
        print("  and re-run before treating these as a real field.")

    return {
        "timing": timing,
        "attribution": attribution,
        "checksums": all_matches,
        "reference_width": reference_width,
        "checksum_bits": checksum_bits,
    }


def emit_layout(frames: Sequence[Frame], analysis: Dict) -> None:
    """Summarise the frame byte by byte, and which label moves each one.

    This used to emit pasteable C++ describing bit shifts and widths. That was the
    right shape while the frame length was unknown, but this protocol turned out to
    be byte-aligned, so the component describes itself with one named constant per
    byte instead. Reporting bytes matches that, and cannot be pasted into a header
    where it would silently disagree with the real structure.
    """
    timing: Timing = analysis["timing"]

    print(banner("FRAME LAYOUT, BYTE BY BYTE"))
    print(
        f"  Timings for YAML:\n"
        f"    header_high:  {timing.header_mark}us\n"
        f"    header_low:   {timing.header_space}us\n"
        f"    bit_high:     {timing.bit_mark}us\n"
        f"    bit_one_low:  {timing.one_space}us\n"
        f"    bit_zero_low: {timing.zero_space}us"
    )
    if timing.gap_space:
        print(f"    mid-frame gap: {timing.gap_space}us")

    byte_frames = [
        (f, bits_to_bytes(f.bits, lsb_first=True)) for f in frames if f.byte_aligned and f.bits
    ]
    if not byte_frames:
        print("\n  Frames are not byte-aligned, so there is no byte layout to report.")
        return

    width = len(byte_frames[0][1])
    if any(len(b) != width for _, b in byte_frames):
        print("\n  Mixed frame lengths; fix the captures before reading anything below.")
        return

    # Credit a label with a byte only when the two frames differ in that label and
    # nothing else. Simply asking which labels co-vary with a byte credits every
    # label at once, because a capture session varies several at different times.
    drivers: Dict[int, List[str]] = {i: [] for i in range(width)}
    for (left, lb), (right, rb) in combinations(byte_frames, 2):
        changed = differing_keys(left.capture.meta, right.capture.meta, STATE_KEYS)
        if len(changed) != 1:
            continue
        for index in range(width):
            if lb[index] != rb[index] and changed[0] not in drivers[index]:
                drivers[index].append(changed[0])

    print(f"\n  {len(byte_frames)} frame(s) of {width} bytes, read least significant bit first.\n")
    for index in range(width):
        values = {b[index] for _, b in byte_frames}
        if len(values) == 1:
            print(f"    byte {index:2d}  constant 0x{values.pop():02X}")
            continue
        summary = ", ".join(drivers[index]) if drivers[index] else "no single label explains it"
        print(f"    byte {index:2d}  varies ({len(values)} values)  <- {summary}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="capture logs, or directories of them")
    parser.add_argument(
        "--bit-order",
        choices=("msb", "lsb", "both"),
        default="both",
        help="byte-packing order to display (default: both)",
    )
    parser.add_argument(
        "--layout", action="store_true", help="print the frame layout byte by byte"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    captures = parse_files(expand_paths(args.paths))
    if not captures:
        print("No raw captures found.", file=sys.stderr)
        return 1

    timing = infer_timing(captures)
    frames = [decode_bits(c, timing) for c in captures]
    frames = [f for f in frames if f.bits]
    if not frames:
        print("Captures parsed but no bits decoded; the timing model is wrong.", file=sys.stderr)
        return 1

    bit_orders = ("msb", "lsb") if args.bit_order == "both" else (args.bit_order,)
    analysis = report(frames, timing, bit_orders, args.verbose)
    if args.layout:
        emit_layout(frames, analysis)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
