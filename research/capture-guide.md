# Capture matrix

Everything in this project depends on getting clean raw IR captures out of the
LG remote. This document is the procedure. Budget about 30 minutes.

You only need this if you are capturing a remote yourself — to *use* the project,
see the [main README](../README.md). Start at the
[research overview](README.md) for why any of this was necessary.

## Before you start

0. Confirm the toolchain works before you touch hardware. There is a synthetic
   session checked in, so this should print an analysis rather than an error:

   ```bash
   python3 research/tools/decode.py research/tools/fixtures/example-session.txt --layout
   ```

   It encodes LG's 28-bit split-system frame, so `frames are not byte-aligned` is
   the expected result — 28 bits is not a whole number of bytes. The fixture is
   there to exercise the pipeline, not to describe this unit.

1. Flash [`../firmware/xiao-ir-capture.yaml`](../firmware/xiao-ir-capture.yaml).
2. **Switch the remote to Celsius.** Hold the `Swing` button on the remote for
   5 seconds (on units without a Swing button, hold `∧` and `∨` together on the
   control panel for 5 seconds). Almost every AC protocol encodes Celsius
   internally, so capturing in Celsius means the numbers you write down are the
   numbers in the frame. It removes an entire class of off-by-one confusion.
3. Point the remote at the top of the IR Mate from about 20 cm away, slightly
   off-axis. This matters more than it sounds. Held up close, a strong emitter
   saturates the receiver's AGC, which then bridges straight over the short
   ~302 µs spaces. That does not look like an error: the two bits either side of
   a swallowed space merge into one double-length mark, so you get a frame that
   parses fine and is quietly wrong. See Troubleshooting for the signature.
4. Open the ESPHome log stream and confirm the level is `DEBUG`. You should see
   `[D][ir]: raw frame: N symbols` **plus a `Received Raw:` block** on every
   press, and the status LED should flash amber. If the `raw frame:` line appears
   without a `Received Raw:` block, `dump` is not set to `raw` — see
   Troubleshooting.
5. Note the symbol count from the first capture. **Every capture of the same
   button must report the same count.** Expect roughly 226 for this remote: a
   112-bit frame is 224 symbols, plus the header pair and the terminator. A count
   that jumps around, or one well below 226, means frames are being merged,
   truncated, or split — see Troubleshooting below.

## The labelling convention

Before each press, type the state into the **Capture Label** text field in Home
Assistant and hit the **Mark Capture** button. Describe the state the unit will be
in **after** the press, since that is what the frame carries — for a cycling
button like Mode, that is the next value, not the current one. Type only the
`key=value` pairs; the firmware adds the `# state:` prefix itself. It writes a
line like

```
# state: mode=cool temp=24 fan=high
```

into the log, which [`tools/parse_raw.py`](tools/parse_raw.py) reads
automatically. Recognised keys:

| Key      | Values                                   | Notes                                        |
| -------- | ---------------------------------------- | -------------------------------------------- |
| `mode`   | `off`, `cool`, `dry`, `fan`, `heat`      | `heat` only exists on `SHR` models           |
| `temp`   | integer Celsius, 16-30                   | omit in `fan` mode                           |
| `fan`    | `low`, `high`                            | omit in `dry` mode, it is not adjustable     |
| `swing`  | `on`, `off`                              | only if your unit has motorised louvres      |
| `light`  | `on`, `dim`, `off`                       | display brightness                           |
| `timer`  | integer hours 1-24, or `off`             |                                              |
| `button` | the physical button pressed              | e.g. `power`, `mode`, `temp_up`, `light`     |

The `button` key matters for the toggle-style buttons (Light, Swing, Timer)
where the interesting thing is the press itself rather than the resulting
state. For the climate state itself, `mode`/`temp`/`fan` are what the decoder
correlates against.

**A label applies to every capture until the next one, including presses you did
not mean to record.** The receiver has no idea which presses are the experiment
and which are you navigating back to a starting state, so the six presses that
walk the setpoint from 30 back to 24 all inherit whatever label was last set.
That is worse than leaving them unlabelled: the decoder sees several different
frames all claiming the same state, declares the protocol non-deterministic, and
mis-attributes fields. Either re-mark before each group, or mark the navigation
presses as you make them — they are perfectly good data once labelled correctly.

One rule that is easy to get wrong: **never drop a key just because it stopped
being meaningful.** The decoder locates a field by finding two captures that
differ in exactly one key, so labelling a power-off as `mode=off` when the
capture before it was `mode=cool temp=24 fan=high` looks like *three* keys
changed and the pair gets discarded. Write `mode=off temp=24 fan=high` instead,
carrying over the values the unit was last set to.

## The matrix

The decoder finds a field by comparing two captures that differ in **exactly
one** key. So the order below is deliberate: each step changes one thing. Don't
skip rows, and don't reorder them.

### A. Determinism check

This needs three frames that *ought* to be byte-identical, so the press has to be
one that changes nothing. `temp_up` only works for that at the top of the range:
raise the setpoint to 30 first, then press `∧` three more times. The remote has
nowhere left to go, so it retransmits the same state.

```
# state: mode=cool temp=30 fan=high button=temp_up
```

x3, then bring it back down to 24.

If the three frames differ, the protocol carries a toggle or sequence counter and
the whole analysis changes, so we need to know that before capturing anything
else. Note that pressing `temp_up` anywhere *below* the maximum is not a
determinism test: each press genuinely changes the setpoint, so the frames are
supposed to differ.

### B. Power

```
# state: mode=off temp=24 fan=high button=power
# state: mode=cool temp=24 fan=high button=power
```

Capture power-off from a known state (Cool / 24 / High), then power back on.
This tells us whether "off" is a distinct command or just a bit in an otherwise
normal frame.

### C. Modes, everything else held constant

Temperature 24 throughout, and starting from Cool.

Mode is a cycle, so mind the off-by-one: the first press does not capture Cool, it
*leaves* Cool and lands on Dry. Label what the unit shows after the press, not
what it was showing when you pressed. Three presses from Cool therefore give:

```
# state: mode=dry temp=24 button=mode
# state: mode=fan temp=24 button=mode
# state: mode=cool temp=24 fan=high button=mode
```

The third one returning to Cool is a free correctness check: it must come out
byte-identical to the Cool frame from section B. If it doesn't, something drifted.

Then, only if your model has it (`LP0721SHR` and similar), one more press:

```
# state: mode=heat temp=24 fan=high button=mode
```

Dry and Fan may force their own fan speed. If you can read it off the remote,
record it; if you can't, leave the `fan` key out entirely rather than guessing.
An absent key is merely skipped, while a wrong one corrupts the attribution.

### D. Temperature sweep, in Cool at Fan High

The sweep needs both ends and a couple of interior points. Powers of two are
useful because they make a positional encoding obvious in the bit dump.

```
# state: mode=cool temp=16 fan=high
# state: mode=cool temp=17 fan=high
# state: mode=cool temp=18 fan=high
# state: mode=cool temp=20 fan=high
# state: mode=cool temp=24 fan=high
# state: mode=cool temp=28 fan=high
# state: mode=cool temp=29 fan=high
# state: mode=cool temp=30 fan=high
```

16 and 17 pin down the offset, 30 pins down the top of the range, and the
interior points confirm it is linear rather than a lookup table.

### E. Fan speed

In Cool at 24:

```
# state: mode=cool temp=24 fan=low
# state: mode=cool temp=24 fan=high
```

Then again in Fan mode, because some protocols encode fan speed differently
when the compressor is off:

```
# state: mode=fan fan=low
# state: mode=fan fan=high
```

### F. Timer

From a powered-on state (Cool / 24 / High):

```
# state: mode=cool temp=24 fan=high timer=1 button=timer
# state: mode=cool temp=24 fan=high timer=2 button=timer
# state: mode=cool temp=24 fan=high timer=12 button=timer
# state: mode=cool temp=24 fan=high timer=24 button=timer
# state: mode=cool temp=24 fan=high timer=off button=timer
```

1 and 2 show where the counter lives, 12 and 24 show its width, and `off`
shows how cancellation is signalled.

Reaching 12 and 24 means a lot of presses, and **every one of them is recorded
whether you label it or not**. Unlabelled frames do not get skipped; they inherit
the last label you marked, so labelling only the milestones above produces a run
of frames all claiming `timer=2`. Mark each press as you go, or accept that the
run will need relabelling from the frame contents afterwards.

Note the timer also moves in whole hours only if you keep pressing briskly — pause
too long and the remote leaves timer-setting mode, so the next press re-enters it
at zero rather than incrementing.

**Already captured.** Byte 9 holds the timer in ten-minute units, `0x06` per hour
up to `0x90` at 24 h, with byte 5 bit `0x08` set while armed. See
[`PROTOCOL.md`](PROTOCOL.md).

### G. Light

Three presses to walk the full On → Dim → Off cycle:

```
# state: mode=cool temp=24 fan=high light=dim button=light
# state: mode=cool temp=24 fan=high light=off button=light
# state: mode=cool temp=24 fan=high light=on button=light
```

If all three frames are identical, Light is a pure toggle and we only need one
raw code for it.

**Already captured, and it is a pure toggle.** Seven presses produced identical
frames setting byte 5 bit `0x40`; the unit cycles the brightness internally and the
frame carries no light value. The power bit stays set, so the
[esphome#2101](https://github.com/esphome/esphome/issues/2101) confusion between
the brightness frame and a power-off frame does not apply here. See
[`PROTOCOL.md`](PROTOCOL.md).

### H. Swing

Only if your unit has motorised louvres. Many LP-series portables have a fixed vane
and no Swing button at all, in which case there is nothing to capture and the
component should not offer a swing control.

```
# state: mode=cool temp=24 fan=high swing=on button=swing
# state: mode=cool temp=24 fan=high swing=off button=swing
```

**Already captured, and unlike Light it is a real state.** Byte 8 holds it in mask
`0x38`, `0` for off and `7` for on, alongside the fan speed in `0x07`. Because the
frame reports it, the climate entity can expose swing as a mode and read it back.
See [`PROTOCOL.md`](PROTOCOL.md).

## Saving the logs

Copy the log text and save it into this directory. One file for everything is
fine — the `# state:` markers do the splitting:

```
research/captures/session-01.txt
```

If you prefer to split by area, use `research/captures/<section>.txt`, e.g.
`research/captures/d-temperature-sweep.txt`. The tools glob the whole directory either
way. Paste the log verbatim; do not reformat, rewrap, or strip the `[D][...]`
prefixes, the parser wants them.

Then:

```bash
python3 research/tools/decode.py research/captures/*.txt
```

## Troubleshooting

**Symbol count varies between presses of the same button, and is below 226.**
Almost certainly receiver saturation from holding the remote too close. The
signature is unmistakable in the raw dump: alongside the normal ~540 µs marks you
get marks at ~1370, ~2220, ~3070 µs and up, each one an exact multiple of
`k × 540 + (k−1) × 302`. Those are runs of *k* bits with the short spaces between
them swallowed. Back off to 20 cm and go further off-axis. If it persists, check
`filter` is well under 302 µs — it is `100us` here, and anything near 250 µs is
close enough to eat real bits.

**Symbol count is a clean multiple of a shorter frame.** Then it really is being
split or doubled, and `idle` is the knob. Raise it if one press arrives as
several captures, lower it if two repeats arrive merged as one. The cap is
32767 µs at the default 1 MHz clock, and proportionally less if you set
`clock_resolution`.

**`raw frame: N symbols` appears but there is no `Received Raw:` block.** `dump`
is set to `all`. ESPHome marks the raw dumper as *secondary*, so it only runs
when no protocol-specific dumper claimed the frame — and the Pronto dumper claims
everything. Set `ir_dump: "raw"`. This is the one failure that produces a log
which looks busy and successful while containing nothing the tools can read.

**No `Received Raw:` lines and no `raw frame:` lines either.** Check the logger is
at `DEBUG`, check the remote's batteries, and confirm you are watching the
receiver on `GPIO4` with `inverted: true`. Point a phone camera at the remote's
emitter to confirm it is actually firing — most phone front cameras show IR as a
faint purple flicker.

**Frames arrive but look like noise.** Increase `tolerance` to `65%`, and move
away from sunlight, fluorescent tubes, and plasma/OLED TVs.

**Nothing gets decoded by the named protocol dumpers.** That is expected and
fine. The raw dump is all the tooling needs.
