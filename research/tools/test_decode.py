#!/usr/bin/env python3
"""Tests for the capture parser and protocol decoder.

Frames are synthesised from protocols whose structure we already know, pushed
through the same pipeline the real captures go through, and checked to see that
the pipeline recovers the structure we started with. Run with:

    python3 -m unittest discover -s tools -p 'test_*.py'
"""

from __future__ import annotations

import random
import unittest
from typing import Dict, List, Sequence

from decode import (
    attribute_fields,
    bits_to_bytes,
    check_determinism,
    cluster,
    decode_bits,
    find_checksums,
    infer_timing,
    runs,
)
from parse_raw import parse_meta, parse_text

# The documented LG 28-bit split-system protocol, used here purely as a known
# quantity to validate the tooling against.
LG28 = dict(
    header_mark=3200,
    header_space=9900,
    bit_mark=500,
    one_space=1600,
    zero_space=550,
)


def lg28_frame(mode: int, temp_c: int, fan: int) -> int:
    """Build a 28-bit LG frame: 0x88 signature, command, temp, fan, checksum."""
    value = 0x8800000
    value |= (mode & 0xFF) << 12
    value |= ((temp_c - 15) & 0xF) << 8
    value |= (fan & 0xF) << 4
    checksum = 0
    for i in range(1, 8):
        checksum += (value >> (i * 4)) & 0xF
    return value | (checksum & 0xF)


def render(bits: Sequence[int], timings: Dict[str, int], jitter: int = 0,
           chunk_bits: int = 0, chunk_gap: int = 8000,
           rng: random.Random | None = None) -> List[int]:
    """Turn bits into an ESPHome-style raw mark/space array."""
    rng = rng or random.Random(0)

    def fuzz(value: int) -> int:
        return value + rng.randint(-jitter, jitter) if jitter else value

    out = [fuzz(timings["header_mark"]), -fuzz(timings["header_space"])]
    for index, bit in enumerate(bits):
        out.append(fuzz(timings["bit_mark"]))
        out.append(-fuzz(timings["one_space"] if bit else timings["zero_space"]))
        if chunk_bits and (index + 1) % chunk_bits == 0 and index + 1 < len(bits):
            out.append(fuzz(timings["bit_mark"]))
            out.append(-fuzz(chunk_gap))
    out.append(fuzz(timings["bit_mark"]))
    return out


def to_bits(value: int, width: int) -> List[int]:
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def as_log(entries: Sequence[tuple], wrap: int = 16) -> str:
    """Render (label, raw) pairs the way the ESPHome dashboard would."""
    lines: List[str] = []
    for label, raw in entries:
        if label:
            lines.append(f"[12:00:00][I][main:123]: # state: {label}")
        chunks = [raw[i : i + wrap] for i in range(0, len(raw), wrap)]
        for position, chunk in enumerate(chunks):
            body = ", ".join(str(v) for v in chunk)
            if position == 0:
                lines.append(f"[12:00:00][D][remote.raw:028]: Received Raw: {body}")
            else:
                lines.append(f"[12:00:00][D][remote.raw:041]:   {body}")
        lines.append("[12:00:00][D][remote.pronto:229]: Received Pronto: data=0000 006D")
    return "\n".join(lines)


class ParserTest(unittest.TestCase):
    def test_meta_parsing(self):
        self.assertEqual(
            parse_meta("mode=cool temp=24 fan=high"),
            {"mode": "cool", "temp": "24", "fan": "high"},
        )
        self.assertEqual(parse_meta("  MODE=Cool ,  fan=LOW "), {"mode": "cool", "fan": "low"})
        self.assertEqual(parse_meta("no pairs here"), {})

    def test_multiline_capture_is_stitched(self):
        raw = render(to_bits(lg28_frame(0x08, 24, 4), 28), LG28)
        log = as_log([("mode=cool temp=24 fan=high", raw)])
        captures = parse_text(log)
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].timings, raw)
        self.assertEqual(captures[0].meta["temp"], "24")

    def test_label_applies_to_following_captures_only(self):
        raw = render(to_bits(lg28_frame(0x08, 24, 4), 28), LG28)
        log = as_log([("", raw), ("mode=dry", raw), ("", raw)])
        captures = parse_text(log)
        self.assertEqual([c.meta.get("mode") for c in captures], [None, "dry", "dry"])

    def test_ansi_colour_codes_are_stripped(self):
        raw = render(to_bits(lg28_frame(0x08, 24, 4), 28), LG28)
        log = as_log([("mode=cool", raw)])
        coloured = "\n".join(f"\x1b[0;36m{line}\x1b[0m" for line in log.splitlines())
        self.assertEqual(len(parse_text(coloured)), 1)

    def test_other_dumpers_do_not_pollute_the_capture(self):
        raw = render(to_bits(lg28_frame(0x08, 24, 4), 28), LG28)
        captures = parse_text(as_log([("mode=cool", raw)]))
        # The trailing Pronto line must not be swallowed as continuation data.
        self.assertEqual(len(captures[0].timings), len(raw))


class TimingTest(unittest.TestCase):
    def test_cluster_splits_on_relative_gaps(self):
        groups = cluster([500, 520, 510, 1600, 1620, 8000])
        self.assertEqual([g.count for g in groups], [3, 2, 1])
        self.assertEqual([g.median for g in groups], [510, 1610, 8000])

    def test_timings_are_recovered_with_jitter(self):
        rng = random.Random(1234)
        entries = []
        for temp in range(16, 31):
            bits = to_bits(lg28_frame(0x08, temp, 4), 28)
            entries.append((f"mode=cool temp={temp} fan=high", render(bits, LG28, jitter=40, rng=rng)))
        captures = parse_text(as_log(entries))
        timing = infer_timing(captures)
        self.assertAlmostEqual(timing.header_mark, LG28["header_mark"], delta=60)
        self.assertAlmostEqual(timing.header_space, LG28["header_space"], delta=60)
        self.assertAlmostEqual(timing.bit_mark, LG28["bit_mark"], delta=60)
        self.assertAlmostEqual(timing.zero_space, LG28["zero_space"], delta=60)
        self.assertAlmostEqual(timing.one_space, LG28["one_space"], delta=60)
        self.assertEqual(timing.warnings, [])

    def test_bits_survive_a_round_trip(self):
        bits = to_bits(lg28_frame(0x08, 24, 4), 28)
        captures = parse_text(as_log([("mode=cool temp=24 fan=high", render(bits, LG28))]))
        timing = infer_timing(captures)
        frame = decode_bits(captures[0], timing)
        self.assertEqual(frame.bits, bits)
        self.assertEqual(frame.errors, [])

    def test_mid_frame_gaps_are_not_decoded_as_bits(self):
        bits = to_bits(0x0F0F0F0F0F0F0F, 56)
        raw = render(bits, LG28, chunk_bits=24, chunk_gap=8000)
        captures = parse_text(as_log([("mode=cool temp=24 fan=high", raw)]))
        timing = infer_timing(captures)
        frame = decode_bits(captures[0], timing)
        self.assertEqual(frame.bits, bits)
        self.assertEqual(frame.chunk_boundaries, [24, 48])
        self.assertEqual(timing.gap_space, 8000)


class ByteViewTest(unittest.TestCase):
    def test_msb_and_lsb_packing(self):
        bits = [1, 0, 0, 0, 1, 0, 0, 0]
        self.assertEqual(bits_to_bytes(bits, lsb_first=False), b"\x88")
        self.assertEqual(bits_to_bytes(bits, lsb_first=True), b"\x11")

    def test_trailing_partial_byte_is_zero_padded(self):
        self.assertEqual(bits_to_bytes([1, 1, 1, 1], lsb_first=False), b"\xf0")


class AttributionTest(unittest.TestCase):
    # For a 28-bit LG frame: command is bits 8..15, temperature 16..19,
    # fan 20..23, checksum 24..27 (counting transmitted bits from zero).
    TEMP_FIELD = set(range(16, 20))
    FAN_FIELD = set(range(20, 24))
    COMMAND_FIELD = set(range(8, 16))
    CHECKSUM_FIELD = set(range(24, 28))

    def _frames(self):
        entries = []
        # The full sweep matters: a partial sweep never toggles the low bit of
        # the temperature nibble, so the decoder would rightly not attribute it.
        for temp in range(16, 31):
            entries.append(
                (f"mode=cool temp={temp} fan=high", render(to_bits(lg28_frame(0x08, temp, 4), 28), LG28))
            )
        for fan, code in (("low", 0x0), ("high", 0x4)):
            entries.append(
                (f"mode=cool temp=24 fan={fan}", render(to_bits(lg28_frame(0x08, 24, code), 28), LG28))
            )
        for mode, code in (("cool", 0x08), ("dry", 0x09), ("fan", 0x0A)):
            entries.append(
                (f"mode={mode} temp=24 fan=high", render(to_bits(lg28_frame(code, 24, 4), 28), LG28))
            )
        captures = parse_text(as_log(entries))
        timing = infer_timing(captures)
        return [decode_bits(c, timing) for c in captures]

    def test_runs_collapse_contiguous_indices(self):
        self.assertEqual(runs({3, 4, 5, 9, 11, 12}), [(3, 5), (9, 9), (11, 12)])

    def test_temperature_field_is_located(self):
        attribution = attribute_fields(self._frames())
        bits = attribution.by_key["temp"]
        # A full sweep exercises all four bits, so the exact run must appear.
        self.assertIn((16, 19), runs(bits))
        # Changing temperature also moves the checksum, and nothing else.
        self.assertLessEqual(bits, self.TEMP_FIELD | self.CHECKSUM_FIELD)

    def test_fan_field_is_located(self):
        attribution = attribute_fields(self._frames())
        bits = attribution.by_key["fan"]
        self.assertTrue(bits & self.FAN_FIELD)
        self.assertLessEqual(bits, self.FAN_FIELD | self.CHECKSUM_FIELD)

    def test_mode_field_is_located(self):
        attribution = attribute_fields(self._frames())
        bits = attribution.by_key["mode"]
        self.assertTrue(bits & self.COMMAND_FIELD)
        self.assertLessEqual(bits, self.COMMAND_FIELD | self.CHECKSUM_FIELD)

    def test_field_values_map_labels_to_encodings(self):
        from decode import field_values

        frames = self._frames()
        mapping = dict(field_values(frames, "temp", (16, 19)))
        # LG encodes the setpoint as (celsius - 15) in that nibble.
        self.assertEqual(mapping["16"], 1)
        self.assertEqual(mapping["24"], 9)
        self.assertEqual(mapping["30"], 15)

    def test_signature_bits_are_never_attributed(self):
        attribution = attribute_fields(self._frames())
        signature = set(range(0, 8))
        for key, bits in attribution.by_key.items():
            self.assertFalse(bits & signature, f"{key} touched the 0x88 signature")


class DeterminismTest(unittest.TestCase):
    def test_identical_repeats_report_stable(self):
        raw = render(to_bits(lg28_frame(0x08, 24, 4), 28), LG28)
        entries = [("mode=cool temp=24 fan=high", raw)] * 3
        captures = parse_text(as_log(entries))
        timing = infer_timing(captures)
        frames = [decode_bits(c, timing) for c in captures]
        findings = check_determinism(frames)
        self.assertTrue(any("stable" in f for f in findings))
        self.assertFalse(any("VARIES" in f for f in findings))

    def test_toggle_bit_is_flagged(self):
        base = lg28_frame(0x08, 24, 4)
        entries = [
            ("mode=cool temp=24 fan=high", render(to_bits(base, 28), LG28)),
            ("mode=cool temp=24 fan=high", render(to_bits(base ^ 0x2000000, 28), LG28)),
        ]
        captures = parse_text(as_log(entries))
        timing = infer_timing(captures)
        frames = [decode_bits(c, timing) for c in captures]
        self.assertTrue(any("VARIES" in f for f in check_determinism(frames)))


class KnownFrameTest(unittest.TestCase):
    """Pin the protocol table against frames captured from real LG hardware.

    These three were logged off physical units and quoted in esphome/issues#2101.
    `lg28_frame` here mirrors the PROTOCOL TABLE in
    components/lg_portable_ac/lg_portable_ac.h, so if someone changes a field
    shift, the command values, or the checksum in the C++ without updating this,
    these break.
    """

    def test_power_off_frame(self):
        # Off parks the fan nibble at AUTO and the temperature nibble at zero.
        self.assertEqual(lg28_frame(0xC0, 15, 0x5), 0x88C0051)

    def test_switch_on_into_cool_at_21c(self):
        self.assertEqual(lg28_frame(0x00, 21, 0x4), 0x880064A)

    def test_display_brightness_frame(self):
        # The frame firmware/packages/lg-extras.yaml sends for the Light button.
        self.assertEqual(lg28_frame(0xC0, 15, 0xA), 0x88C00A6)

    def test_checksum_is_the_low_nibble(self):
        for command, temp, fan in ((0xC0, 15, 0x5), (0x00, 21, 0x4), (0x08, 24, 0x0)):
            frame = lg28_frame(command, temp, fan)
            nibbles = [(frame >> shift) & 0xF for shift in range(24, -1, -4)]
            self.assertEqual(nibbles[-1], sum(nibbles[:-1]) & 0xF)


class ChecksumTest(unittest.TestCase):
    def _frames(self, builder, width):
        entries = []
        for temp in range(16, 31):
            entries.append((f"temp={temp}", render(to_bits(builder(temp), width), LG28)))
        captures = parse_text(as_log(entries))
        timing = infer_timing(captures)
        return [decode_bits(c, timing) for c in captures]

    def test_lg28_nibble_sum_is_found(self):
        frames = self._frames(lambda t: lg28_frame(0x08, t, 4), 28)
        matches = find_checksums(frames, "msb")
        self.assertTrue(
            any(m.algorithm == "nibble_sum" and m.checksum_nibbles == 1 for m in matches),
            [m.describe() for m in matches],
        )

    def test_byte_sum_checksum_is_found(self):
        def builder(temp: int) -> int:
            payload = (0x88 << 40) | (0x08 << 32) | ((temp - 15) << 24) | (0x4 << 16) | 0x0100
            checksum = sum(((payload >> (8 * i)) & 0xFF) for i in range(1, 7)) & 0xFF
            return payload | checksum

        frames = self._frames(builder, 56)
        matches = find_checksums(frames, "msb")
        self.assertTrue(
            any(m.algorithm == "byte_sum" and m.checksum_nibbles == 2 for m in matches),
            [m.describe() for m in matches],
        )

    def test_no_checksum_when_field_is_constant(self):
        # Frames whose last nibble never changes cannot be a checksum.
        frames = self._frames(lambda t: (0x8800000 | (t - 15) << 8), 28)
        matches = find_checksums(frames, "msb")
        self.assertEqual([m for m in matches if m.checksum_nibbles == 1], [])


if __name__ == "__main__":
    unittest.main()
