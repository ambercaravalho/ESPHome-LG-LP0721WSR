# Capture matrix

Everything in this project depends on getting clean raw IR captures out of the
LG remote. This document is the procedure. Budget about 30 minutes.

## Before you start

0. Confirm the toolchain works before you touch hardware. There is a synthetic
   session checked in, so this should print a full analysis:

   ```bash
   python3 tools/decode.py tools/fixtures/example-session.txt --emit-cpp
   ```

1. Flash [`../firmware/xiao-ir-capture.yaml`](../firmware/xiao-ir-capture.yaml).
2. **Switch the remote to Celsius.** Hold the `Swing` button on the remote for
   5 seconds (on units without a Swing button, hold `∧` and `∨` together on the
   control panel for 5 seconds). Almost every AC protocol encodes Celsius
   internally, so capturing in Celsius means the numbers you write down are the
   numbers in the frame. It removes an entire class of off-by-one confusion.
3. Point the remote at the top of the IR Mate from about 20 cm away, slightly
   off-axis. Pressed directly against the receiver, a strong emitter can
   saturate it and distort the mark/space widths.
4. Open the ESPHome log stream and confirm the level is `DEBUG`. You should see
   `[D][ir]: raw frame: N symbols` plus a `Received Raw:` block on every press,
   and the status LED should flash amber.
5. Note the symbol count from the first capture. **Every capture of the same
   button must report the same count.** A count that jumps around means frames
   are being truncated or split — see Troubleshooting below.

## The labelling convention

Before each press, type the state into the **Capture Label** text field in Home
Assistant and hit the **Mark Capture** button. That writes a line like

```
# state: mode=cool temp=24 fan=high
```

into the log, which [`../tools/parse_raw.py`](../tools/parse_raw.py) reads
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

Press the same button three times without changing anything. All three frames
must be byte-identical. If they aren't, the protocol carries a toggle or
sequence bit and the whole analysis changes, so we need to know up front.

```
# state: mode=cool temp=24 fan=high button=temp_up
```

x3, then set it back to 24.

### B. Power

```
# state: mode=off temp=24 fan=high button=power
# state: mode=cool temp=24 fan=high button=power
```

Capture power-off from a known state (Cool / 24 / High), then power back on.
This tells us whether "off" is a distinct command or just a bit in an otherwise
normal frame.

### C. Modes, everything else held constant

Fan speed High, temperature 24 throughout.

```
# state: mode=cool temp=24 fan=high button=mode
# state: mode=dry fan=low button=mode
# state: mode=fan fan=high button=mode
```

Then, only if your model has it (`LP0721SHR` and similar):

```
# state: mode=heat temp=24 fan=high button=mode
```

Note that Dry forces its own fan speed, so record whatever the remote shows.

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
shows how cancellation is signalled. Reaching 12 and 24 means a lot of button
presses — mark the label only for the presses listed above and ignore the
intermediate ones, the decoder skips unlabelled frames.

### G. Light

Three presses to walk the full On → Dim → Off cycle:

```
# state: mode=cool temp=24 fan=high light=dim button=light
# state: mode=cool temp=24 fan=high light=off button=light
# state: mode=cool temp=24 fan=high light=on button=light
```

If all three frames are identical, Light is a pure toggle and we only need one
raw code for it.

### H. Swing

Only if your unit has motorised louvres:

```
# state: mode=cool temp=24 fan=high swing=on button=swing
# state: mode=cool temp=24 fan=high swing=off button=swing
```

## Saving the logs

Copy the log text and save it into this directory. One file for everything is
fine — the `# state:` markers do the splitting:

```
captures/session-01.txt
```

If you prefer to split by area, use `captures/<section>.txt`, e.g.
`captures/d-temperature-sweep.txt`. The tools glob the whole directory either
way. Paste the log verbatim; do not reformat, rewrap, or strip the `[D][...]`
prefixes, the parser wants them.

Then:

```bash
python3 tools/decode.py captures/*.txt
```

## Troubleshooting

**Symbol count varies between presses of the same button.** The frame is being
split. Raise `idle` in `packages/xiao-hardware.yaml` (the cap is 65534 µs at
`clock_resolution: "500000"`). If it is being truncated instead, raise
`rmt_symbols` from the ESP32-C3 default of 96 to 192, and `buffer_size` to
`20000b`.

**No `Received Raw:` lines at all.** Check the logger is at `DEBUG`, check the
remote's batteries, and confirm you are watching the receiver on `GPIO4` with
`inverted: true`. Point a phone camera at the remote's emitter to confirm it is
actually firing — most phone front cameras show IR as a faint purple flicker.

**Frames arrive but look like noise.** Increase `tolerance` to `65%`, and move
away from sunlight, fluorescent tubes, and plasma/OLED TVs.

**Nothing gets decoded by the named protocol dumpers.** That is expected and
fine. The raw dump is all the tooling needs.
