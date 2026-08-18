# Research

**You do not need anything in this folder to use the project.** The protocol is
already measured and built into the component, so flashing the firmware is enough.
This is the evidence behind it, kept so the claims are checkable and so the work is
repeatable on a remote nobody has captured yet.

## What is here

| File | What it is |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | The frame format, field by field, with the evidence for each claim and the reasoning where a claim is uncertain |
| [capture-guide.md](capture-guide.md) | The capture procedure: which buttons to press, in what order, and why that order matters |
| [captures/](captures/) | The raw logs. 90 captures of the original remote, exactly as ESPHome printed them |
| [tools/](tools/) | The analysis scripts, all standard-library Python |

Read `PROTOCOL.md` if you want to know what the protocol *is*. Read this page if
you want to know how it was found, or you are about to capture a remote yourself.

## Why the protocol had to be measured

ESPHome's `climate_ir_lg` and Home Assistant's LG integration both target LG's
*split-system* remotes: a 28-bit frame behind an 8000/4000 µs header. When an LG
remote does not respond, the standard advice is to retune the header to the
3200/9900 µs some models use.

That advice cannot help here. This remote sends **112 bits behind a 3150/1590 µs
header** — four times the frame length, with a header space off by a factor of six.
A decoder looking for the LG protocol does not misread these frames, it discards
them, which is indistinguishable from "your device is unsupported". No amount of
configuration closes a gap that large, so the only way forward was to capture the
remote and read the bits.

## How the analysis works

The two scripts do different jobs, and the distinction is the useful part.

**`tools/decode.py` discovers structure.** It takes labelled raw captures and
infers the framing, the bit encoding, the byte order, which bytes carry which
field, and which checksum algorithms are still consistent with the evidence. It
reports what fits rather than asserting an answer, so it gets *less* uncertain as
you feed it more captures.

**`tools/verify_protocol.py` asserts structure.** Once a field is understood, this
is what stops it silently regressing. It re-checks every documented claim against
every capture, cross-checks each frame against its label, and — the part that
matters most — rebuilds each frame from only the fields the component models and
requires a byte-for-byte match. A field nobody noticed therefore fails the check,
rather than quietly producing transmissions that differ from the remote's.

```bash
# Prove the toolchain works, using a synthetic session that needs no hardware.
# It encodes LG's 28-bit split-system frame, so "frames are not byte-aligned" in
# the output is correct rather than a failure: 28 bits is not a whole number of
# bytes. It is there to exercise the pipeline, not to describe this unit.
python3 research/tools/decode.py research/tools/fixtures/example-session.txt --layout
python3 -m unittest discover -s research/tools -p 'test_*.py'

# Re-derive the protocol from the committed captures.
python3 research/tools/decode.py research/captures/ --layout

# Check the committed captures against everything PROTOCOL.md claims.
python3 research/tools/verify_protocol.py
```

The last command is the one to run after touching the protocol. It should print
`all consistent`.

## Capturing your own remote

Worth doing if your unit behaves differently from what is documented, or you have
another LP-series model and want to extend this.

1. Flash the capture rig: `esphome run firmware/xiao-ir-capture.yaml`.
2. Work through [capture-guide.md](capture-guide.md). Budget about 30 minutes.
3. Save the logs into `research/captures/` and analyse them.

Two things in that guide are easy to skip and expensive to get wrong, so they are
worth repeating here.

**Do not hold the remote close to the receiver.** About 20 cm, slightly off-axis. A
strong emitter held up close saturates the receiver's AGC, which then bridges over
the short ~290 µs spaces. This does not look like an error: the two bits either
side of a swallowed space merge into one double-length mark, so the frame parses
cleanly and is quietly wrong. A symbol count that varies between presses of the
same button is the tell.

**Labels describe the state after the press.** The decoder locates a field by
comparing two captures that differ in exactly one thing, so a label that is right
about the wrong frame is worse than no label. Mark every press, including the
intermediate ones while walking the temperature up — an unmarked press inherits the
previous label and silently misattributes whichever field you were not watching.

## What is still unknown

Four things, listed at the end of [PROTOCOL.md](PROTOCOL.md). All of them are
reachable only by transmitting frames the remote cannot produce, so they need the
component's `send_raw_frame` action and a willingness to watch how the unit reacts:
fixed vane angles, sub-hour timer values, byte 8's `0x40` bit, and the three bytes
that never moved across all 90 captures.

None of them block normal use.
