# LG LP0721WSR IR protocol

## Status

Measured from 90 captures of the original remote, recorded with
[`firmware/xiao-ir-capture.yaml`](firmware/xiao-ir-capture.yaml) and analysed with
[`tools/decode.py`](tools/decode.py). The raw session is committed as
[`captures/session-01.txt`](captures/session-01.txt) so every claim here is
reproducible.

| Item | Status |
| --- | --- |
| Framing and pulse timings | **Measured**, 90 captures |
| Frame length: 112 bits / 14 bytes | **Measured** |
| Byte order: LSB-first | **Measured** — the checksum only closes in this reading |
| Statefulness: every frame carries the full state | **Measured**, no toggle or sequence bit |
| Constant prefix, bytes 0–4 | **Measured** |
| Power bit | **Measured** |
| Mode field, Cool / Dry / Fan | **Measured** |
| Temperature field, 16–30 °C | **Measured** across the entire range |
| Checksum | **Measured** — a plain byte sum, now the only algorithm that fits |
| Fan field | **Measured** — two speeds, which is all this unit has |
| Timer, 1–24 h | **Measured** across the entire range |
| Light | **Measured** — a pure toggle, one frame, no readable state |
| Swing | **Measured** — a real state in byte 8, on and off |
| Fahrenheit setpoint | **Measured** — byte 12, acted on by the unit, finer than 1 °C |
| Byte 8 bit `0x40` | **Observed**; probably the remote's timer-setting mode, see below |

## Why the stock component cannot work

Worth recording, since "the official LG integration doesn't work" is what started
this. It is not a configuration problem and no amount of tuning fixes it.

ESPHome's `climate_ir_lg` implements a 28-bit frame with an 8000/4000 µs header.
Some LG remotes instead use roughly 3200/9900 µs, which is the usual first thing
to try. This remote is neither: **112 bits** with a **3150/1590 µs** header. It is
four times the frame length, and the header space is off by a factor of six. A
receiver looking for the LG protocol does not misdecode these frames, it discards
them, which presents identically to "unsupported".

## Framing

38 kHz carrier assumed — standard for this class, but note that `remote_receiver`
does not measure carrier frequency, so this is the one number here that is
inherited rather than observed.

| Element | Measured | Notes |
| --- | --- | --- |
| Header mark | ~3150 µs | |
| Header space | ~1590 µs | Not the ~9900 µs of other LG models |
| Bit mark | ~550 µs | Constant; the bit value is in the space |
| Space, `0` | ~290 µs | |
| Space, `1` | ~1070 µs | |
| Payload | 112 bits | 226 symbols, plus header pair and terminator = 228 |
| Mid-frame gaps | none | Max non-terminal space observed is ~1100 µs |

A capture that reports anything other than 228 symbols is damaged. The usual
cause is holding the remote too close, which saturates the receiver and merges
adjacent bits; see the troubleshooting section of
[`captures/README.md`](captures/README.md).

## Frame layout

Each byte is transmitted least significant bit first. Every byte below is given in
that reading, which is the one where the checksum closes — the raw MSB-first view
is only useful for spotting the bit order in the first place.

```
  byte   0  1  2  3  4   5     6     7     8     9    10 11 12   13
        23 CB 26 01 00  flg  mode  temp  fan+  timer  00 00 00  cksum
        \------------/  \--------------------------/            \--/
          constant                  state                     checksum

  byte 5   . f . . t . p .        byte 8   . m s s s f f f
           0x40 light             0x40 remote timer mode
           0x08 timer armed       0x38 swing
           0x04 power             0x07 fan
           0x20 always set
```

| Byte | Field | Encoding |
| --- | --- | --- |
| 0–4 | Constant | Always `23 CB 26 01 00` |
| 5 | Power + flags | Base `0x20`; `0x04` = on, `0x08` = timer armed, `0x40` = Light press |
| 6 | Mode | Cool `0x03`, Dry `0x02`, Fan `0x07` |
| 7 | Temperature | `31 − °C`, so 16 °C is `0x0F` and 30 °C is `0x01` |
| 8 | Fan, swing, timer mode | Fan `0x07`: Low `0x02`, High `0x05`. Swing `0x38`: off `0`, on `7` |
| 9 | Timer | Ten-minute units: hours × 6. `0` = off |
| 10–11 | Constant | Zero in all 90 captures |
| 12 | Fahrenheit setpoint | `0` in Celsius mode, else `0x80 \| °F` |
| 13 | Checksum | See below |

Power and mode are independent: an off frame keeps the mode, temperature and fan
bytes of the state it was in and only clears `0x04` in byte 5. So the component
sets power and mode separately rather than needing a combined command table, which
is what the previous hypothesis assumed.

The temperature encoding is confirmed across all fifteen values. Note it counts
*down*: 16 °C is `0x0F`, 30 °C is `0x01`. Pressing `∨` at 16 or `∧` at 30 is
idempotent and retransmits the same frame, which is a convenient way to get
repeated identical frames for a determinism check.

## Timer

Three things move together when the timer is set:

| | Timer off | Timer set to *h* hours |
| --- | --- | --- |
| Byte 5 | `0x24` | `0x2C` — bit `0x08` set |
| Byte 9 | `0x00` | `h × 6` |
| Byte 8 | `0x05` | `0x45` — bit `0x40` set |

Byte 9 counts **ten-minute units**, not hours: each press of Timer advanced it by
exactly 6, from `0x06` at 1 hour to `0x90` at 24 hours. The remote only offers
whole hours, so only multiples of 6 were ever transmitted. Whether the unit honours
an intermediate value such as `0x03` for 30 minutes is untested — the remote cannot
produce one, so it can only be answered by transmitting a frame the remote never
sends. Worth trying, since it would make the Home Assistant timer usefully finer
than the physical remote.

At 24 hours the next press rolls over to a frame byte-for-byte identical to the
first Timer press, with byte 9 back to `0x00` and byte 5's `0x08` cleared. So zero
genuinely means off; there is no separate disarm command.

Byte 8's `0x40` bit rides along on every frame sent while the timer is being set,
making byte 8 read `0x45` rather than `0x05`. It is **not** the "timer armed" flag —
byte 5's `0x08` is that, and `0x40` stayed set on the rolled-over frame where the
timer was already off.

It appears to be the remote's own timer-setting mode rather than anything about the
AC's state. Eleven minutes elapsed between the last Timer press and the first Light
press with no button touched in between, and by then the bit had cleared on its
own. A remote-side UI mode that times out explains that; a field describing the
unit's configuration does not.

That leaves it likely optional for transmission, but unverified: no capture yet
shows a frame with byte 5's `0x08` set while byte 8's `0x40` is clear, so we cannot
yet prove the AC accepts an armed timer without it. Arming a timer, waiting for the
remote to leave timer mode, then pressing `∧` would produce exactly that frame and
settle it.

Only `0x45` was captured, never `0x42`, because every timer capture was at fan
High. A frame combining the timer with fan Low is therefore predicted rather than
observed.

## Fahrenheit display

Switching the remote's display to Fahrenheit brings byte 12 to life. It is zero in
Celsius mode and `0x80 | °F` otherwise:

```
75F  23 CB 26 01 00 24 03 07 05 00 00 00 CB 13
76F  23 CB 26 01 00 24 03 07 05 00 00 00 CC 14
77F  23 CB 26 01 00 24 03 06 05 00 00 00 CD 14
78F  23 CB 26 01 00 24 03 05 05 00 00 00 CE 14
```

Byte 7 keeps carrying the Celsius setpoint throughout, rounded: 75F and 76F both
map to 24C, 77F to 25C, 78F to 26C. The two bytes therefore agree but are not
redundant, because **1 °F is a finer step than 1 °C**. Four consecutive presses
produce four distinct byte 12 values but only three distinct byte 7 values.

This was briefly mistaken for a sequence counter, which is worth recording as a
caution: byte 12 incremented by exactly one on each of four consecutive frames,
which looks conclusive until you notice one press equals one degree Fahrenheit. A
per-frame counter would have contradicted the statefulness the rest of this
document depends on, so the distinction matters.

The interesting consequence is that Fahrenheit mode is the finer-grained control
surface, giving roughly 0.56 °C resolution instead of 1 °C.

The component transmits byte 12, and this is **confirmed on hardware**: asking for
78 °F from Home Assistant produced

```
     23 CB 26 01 00 24 03 05 3A 00 00 00 CE 49
```

and the unit's display switched to Fahrenheit and read exactly 78, not the 79 it
would have shown had it converted 26 °C back. So the unit does act on byte 12, and
byte 12 also drives which unit the display shows.

The trigger is worth explaining, because the obvious rule is wrong. Home Assistant
always sends Celsius, converting first when its own units are Fahrenheit, so 78 °F
arrives as 25.56 °C — a value that is not a whole degree Celsius, which is the tell.
But 77 °F is exactly 25.00 °C, so a per-frame test would fall back to Celsius at
that one value and flip the display back and forth as the setpoint crossed it.
The component therefore *latches*: any evidence of Fahrenheit turns it on, and only
a received frame with byte 12 clear turns it off. That also lets the physical
remote's unit button take precedence, which is what pressing it is asking for.

## Swing

Unlike Light, swing is a real state that the frame reports, so the climate entity
can expose it as a swing mode and read it back rather than firing blind:

```
swing on   23 CB 26 01 00 24 03 07 3D 00 00 00 00 80
swing off  23 CB 26 01 00 24 03 07 05 00 00 00 00 48
```

Byte 8 goes from `0x05` to `0x3D`, which is the same fan value with `0x38` added.
That is three adjacent bits moving together, so byte 8 is packed rather than being
the fan byte the earlier captures made it look like:

| Mask | Meaning |
| --- | --- |
| `0x07` | Fan — Low `0x02`, High `0x05` |
| `0x38` | Swing — `0` off, `7` on |
| `0x40` | The remote's timer-setting mode, above |
| `0x80` | Never observed set |

Reading swing as a three-bit field rather than three independent flags is a guess,
but a well-motivated one: LG's vertical-swing field conventionally holds a louvre
position, with the all-ones value meaning "sweep continuously" and lower values
selecting fixed angles. If that holds here, values `1`–`6` would be fixed vane
positions that the remote has no button for, reachable only by transmitting them.
That would be a genuine feature gain over the physical remote, and it is safe to
probe since an unsupported value is most likely ignored.

## Light

A pure toggle, and the simplest field here. Seven consecutive presses produced
byte-identical frames:

```
23 CB 26 01 00 64 03 07 05 00 00 00 00 88
```

Byte 5 becomes `0x64`, which is the normal `0x24` plus bit `0x40`. Everything else
is the ordinary current state. The frame carries no light *value*: the unit walks
On → Dim → Off internally, one step per frame received, so there is nothing to
read back and no way to command a specific brightness. A button entity is the
honest mapping, not a select.

Worth recording that the trap this was checked for does not apply. On some LG
protocols the display-brightness frame is a near-copy of the power-off frame,
differing by one nibble, so a decoder can turn the unit off when the user dims the
display ([esphome#2101](https://github.com/esphome/esphome/issues/2101)). Here the
Light frame keeps the power bit `0x04` set and adds a bit of its own, so the two
are never one nibble apart and cannot be confused.

## Checksum

```
byte13 = (byte0 + byte1 + ... + byte12) & 0xFF
```

Consistent across all 90 captures, and now the **only** algorithm that fits. Three
worked examples:

```
Cool 30C        23 CB 26 01 00 24 03 01 05 00 00 00 00  -> sum 0x142 -> 0x42
Off  24C        23 CB 26 01 00 20 03 07 05 00 00 00 00  -> sum 0x144 -> 0x44
Cool 24C 24h    23 CB 26 01 00 2C 03 07 45 90 00 00 00  -> sum 0x220 -> 0x20
```

The timer is what settled this. Until then `nibble_sum` fitted just as well,
because every byte that varied had a zero high nibble (`0x03`, `0x07`, `0x05`) and
for such bytes the value *equals* its nibble sum. The two can only diverge once a
varying byte exceeds `0x0F`, which nothing did until byte 9 reached `0x10` and
byte 8 became `0x45`. At 24 hours the gap is decisive: the byte sum gives `0x20`
and matches, while `nibble_sum` predicts `0x8C`.

`decode.py` reports this as one algorithm in three equivalent forms. The other two
start at nibble 2 or 4 and add `0x23` or `0xEE` straight back — the same sum with
part of the constant prefix omitted and folded into the offset. Those are not
rival theories and no further capture can separate them, because the bytes they
skip never change. The tool used to list them as separate hypotheses and advise
capturing more states, which was advice that could never be satisfied.

## What is left

Every button on the remote is now accounted for. What remains is all reachable only
by transmitting frames the remote cannot produce, so it belongs to hardware testing
rather than capture:

1. **Fixed vane positions.** Whether swing values `1`–`6` select louvre angles, as
   the field width suggests.
2. **Sub-hour timer values.** Byte 9 is in ten-minute units but the remote only
   emits multiples of 6. Transmitting `0x03` would show whether the unit accepts
   30 minutes.
3. **Byte 8 bit `0x40`.** Whether an armed timer is accepted without it. See the
   test described above.
4. **Heat.** Not applicable to the LP0721WSR, which is cooling only. The mode
   nibble has room for it on `SHR` variants.
5. **Bytes 10–11, and byte 8's `0x80`.** Constant across all 90 captures. Nothing
   on this remote drives them.

## Escape hatch

Light, Swing and the Timer are all first-class now, so this is no longer needed for
them. It exists for the untested corners above — a fixed vane position, a half-hour
timer, a Fahrenheit setpoint — and for anything a different LP-series remote turns
out to send:

```yaml
button:
  - platform: template
    name: "Vane position 3"
    on_press:
      # Cool, 24C, fan High, swing field set to 3 instead of 0 or 7.
      - lg_portable_ac.send_raw_frame:
          id: ac
          frame: [0x23, 0xCB, 0x26, 0x01, 0x00, 0x24, 0x03, 0x07, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x00]
```

All fourteen bytes are required, since 112 bits does not fit in an integer.
`send_raw_frame` recomputes the checksum by default, so the last byte can be left
at zero; pass `recalculate_checksum: false` to transmit a captured frame byte for
byte. Note that a raw frame is transmitted as given and does not update the climate
entity, so the entity will disagree with the unit until the next state change.
