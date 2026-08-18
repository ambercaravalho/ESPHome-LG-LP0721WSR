# LG LP0721WSR IR protocol

## Status

Measured from 40 captures of the original remote, recorded with
[`firmware/xiao-ir-capture.yaml`](firmware/xiao-ir-capture.yaml) and analysed with
[`tools/decode.py`](tools/decode.py). The raw session is committed as
[`captures/session-01.txt`](captures/session-01.txt) so every claim here is
reproducible.

| Item | Status |
| --- | --- |
| Framing and pulse timings | **Measured**, 40 captures |
| Frame length: 112 bits / 14 bytes | **Measured** |
| Byte order: LSB-first | **Measured** — the checksum only closes in this reading |
| Statefulness: every frame carries the full state | **Measured**, no toggle or sequence bit |
| Constant prefix, bytes 0–4 | **Measured** |
| Power bit | **Measured** |
| Mode field, Cool / Dry / Fan | **Measured** |
| Temperature field, 16–30 °C | **Measured** across the entire range |
| Checksum | **Measured** and consistent across all 40 frames, though not yet unique |
| Fan field | Located, values **not** yet resolved |
| Timer, Light, Swing | Not yet captured |

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
  byte   0  1  2  3  4   5     6     7     8   9 10 11 12   13
        23 CB 26 01 00  pwr  mode  temp  fan   00 00 00 00  cksum
        \------------/  \--------------------/              \--/
          constant            state                       checksum
```

| Byte | Field | Encoding |
| --- | --- | --- |
| 0–4 | Constant | Always `23 CB 26 01 00` |
| 5 | Power | `0x24` on, `0x20` off — only bit `0x04` moves |
| 6 | Mode | Cool `0x03`, Dry `0x02`, Fan `0x07` |
| 7 | Temperature | `31 − °C`, so 16 °C is `0x0F` and 30 °C is `0x01` |
| 8 | Fan | `0x05` at High in Cool; `0x02` in Dry and Fan mode |
| 9–12 | Constant | Always zero in everything captured so far |
| 13 | Checksum | See below |

Power and mode are independent: an off frame keeps the mode, temperature and fan
bytes of the state it was in and only clears `0x04` in byte 5. So the component
sets power and mode separately rather than needing a combined command table, which
is what the previous hypothesis assumed.

The temperature encoding is confirmed across all fifteen values. Note it counts
*down*: 16 °C is `0x0F`, 30 °C is `0x01`. Pressing `∨` at 16 or `∧` at 30 is
idempotent and retransmits the same frame, which is a convenient way to get
repeated identical frames for a determinism check.

## Checksum

```
byte13 = (byte0 + byte1 + ... + byte12) & 0xFF
```

Consistent across all 40 captures. Two worked examples:

```
Cool 30C  23 CB 26 01 00 24 03 01 05 00 00 00 00  -> sum 0x142 -> 0x42
Off  24C  23 CB 26 01 00 20 03 07 05 00 00 00 00  -> sum 0x144 -> 0x44
```

`decode.py` still reports eight hypotheses that fit, but they are not eight
independent theories:

- Variants starting at nibble 1, 2, 3 or 4 are the same sum with part of the
  constant prefix omitted, and the difference absorbed into a constant offset.
- `nibble_sum` survives alongside `byte_sum` because every byte that actually
  varies has a zero high nibble (`0x03`, `0x07`, `0x05`), and for such bytes the
  value equals the nibble sum. The two only diverge once a varying byte exceeds
  `0x0F`.

So the plain byte sum with no offset is the only candidate needing no unexplained
constant, and a timer value of 1–24 hours is the field most likely to finally
separate it from `nibble_sum`.

## What is left

1. **Fan values.** Byte 8 is `0x05` at High in Cool and `0x02` in both Dry and Fan
   mode. Whether `0x02` is Low, Auto, or mode-forced needs section E.
2. **Timer.** Unknown, and probably where bytes 9–12 stop being zero. Also the
   likeliest way to pin the checksum down uniquely.
3. **Light and Swing.** Unknown. If each press produces an identical frame they
   are pure toggles and one captured frame each is enough.
4. **Heat.** Not applicable to the LP0721WSR, which is cooling only. The mode
   nibble has room for it on `SHR` variants.
5. **Bytes 9–12.** Constant zero so far. Timer or swing state is the obvious
   candidate for what lives there.

## Escape hatch

Any frame you discover that does not fit the climate entity can be bound to a
button without touching C++:

```yaml
button:
  - platform: template
    name: "Display Brightness"
    on_press:
      - lg_portable_ac.send_frame:
          id: ac
          frame: 0x...
```

`send_frame` recomputes the checksum by default. Pass
`recalculate_checksum: false` to transmit a captured frame byte for byte.
[`firmware/packages/lg-extras.yaml`](firmware/packages/lg-extras.yaml) uses this
for Light, Swing and the Timer.
