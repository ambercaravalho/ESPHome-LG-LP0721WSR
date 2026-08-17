# LG LP0721WSR IR protocol

## Status

**The frame layout implemented in `components/lg_portable_ac/` is a hypothesis,
not a measured result.** It is the documented LG air-conditioner layout, which
is field-tested against LG split systems but has not been confirmed against an
LP-series portable. Nobody appears to have published a decode for this remote.

| Item | Status |
| --- | --- |
| Frame layout (signature, command, temp, fan, checksum) | Hypothesis, from LG split systems |
| Command byte values | Hypothesis, from LG split systems |
| Temperature encoding `celsius - 15` | Hypothesis, from LG split systems |
| Checksum: 4-bit sum of the preceding nibbles | Hypothesis, from LG split systems |
| Pulse timings | Hypothesis, best guess for the LG "long header" family |
| Everything above, for **your** unit | **Unverified. Capture required.** |

Finishing this is mechanical, not exploratory: work through
[`captures/README.md`](captures/README.md), run `tools/decode.py`, and either
confirm the table below or replace it. The tool prints a drop-in replacement.

## Try this first: it may already work

Before writing anything off, rule out the boring explanation. ESPHome's built-in
`climate_ir_lg` defaults to an **8000 µs / 4000 µs** header. A large share of LG
remotes, including the portables, use roughly **3200 µs / 9900 µs** instead. A
component with the wrong header timings does not misbehave, it simply gets
ignored, which looks exactly like "unsupported protocol".

So try the stock component with the other header first:

```yaml
climate:
  - platform: climate_ir_lg
    name: "Portable AC"
    transmitter_id: ir_tx
    receiver_id: ir_rx
    header_high: 3200us
    header_low: 9900us
```

If the unit responds, you are done and you do not need this repo's component at
all. If it does not, continue below. Either way the capture is worth doing,
because it is the only thing that turns a guess into a fact.

## The hypothesised frame

28 bits, most significant bit transmitted first, 38 kHz carrier.

```
  bit index   0        8               16       20       24
              |        |               |        |        |
              +--------+---------------+--------+--------+
              |  0x88  |    command    |  temp  |  fan   | cksum
              +--------+---------------+--------+--------+
  width          8            8           4        4        4
```

Expressed as shifts from the least significant bit, which is how
`lg_portable_ac.h` stores them:

| Field | Shift | Width | Notes |
| --- | --- | --- | --- |
| Signature | `frame_bits - 8` | 8 | Always `0x88` |
| Command | 12 | 8 | Power state and mode combined |
| Temperature | 8 | 4 | `celsius - 15`, so 16 °C is `0x1` and 30 °C is `0xF` |
| Fan | 4 | 4 | |
| Checksum | 0 | 4 | |

### Command byte

LG distinguishes "switch on into mode X" from "change to mode X while already
running", so the same requested mode produces different frames depending on what
the unit was doing before. The component tracks this in `mode_before_`.

| Command | Value | Meaning |
| --- | --- | --- |
| `CMD_OFF` | `0xC0` | Power off |
| `CMD_ON_COOL` | `0x00` | Switch on into Cool |
| `CMD_ON_DRY` | `0x01` | Switch on into Dry |
| `CMD_ON_FAN_ONLY` | `0x02` | Switch on into Fan |
| `CMD_ON_HEAT` | `0x04` | Switch on into Heat |
| `CMD_COOL` | `0x08` | Change to Cool while running |
| `CMD_DRY` | `0x09` | Change to Dry while running |
| `CMD_FAN_ONLY` | `0x0A` | Change to Fan while running |
| `CMD_HEAT` | `0x0C` | Change to Heat while running |
| `CMD_SWING` | `0x10` | Toggle louvre swing |

### Fan nibble

| Speed | Value | Used by this unit |
| --- | --- | --- |
| Low | `0x0` | yes |
| Medium | `0x2` | no, LP-series has two speeds |
| High | `0x4` | yes |
| Auto | `0x5` | transmitted only in the power-off frame |

### Checksum

Sum every nibble above the checksum field, keep the low 4 bits:

```
checksum = (n0 + n1 + n2 + n3 + n4 + n5) & 0xF
```

`tools/decode.py` searches a wider space than this (nibble/byte, sum/xor,
several payload start offsets, plus a constant offset) and reports every
hypothesis that fits, so if the real algorithm differs it will say so.

## Open questions the captures will settle

1. **Is the frame really 28 bits?** LG portables have also been reported at 48
   and 56 bits. Set `frame_bits:` accordingly; the field shifts are measured
   from the end of the frame precisely so that a longer frame does not
   invalidate them.
2. **Are there mid-frame gaps?** Some LG AC frames are split into chunks
   separated by an 8-10 ms space. If `decode.py` reports a mid-frame gap, set
   `chunk_bits:` and `chunk_gap_low:`.
3. **Is it LSB-first?** Byte-oriented AC protocols usually transmit each byte
   least significant bit first. The 28-bit LG frame is not byte-aligned and is
   MSB-first. `decode.py` shows both views and only trusts the one whose
   checksum is self-consistent. If yours turns out to be LSB-first and
   byte-aligned, the component's bit loop needs to reverse each byte, which is
   the one change that does require editing C++.
4. **Does Dry carry a setpoint?** The owner's manual says the up/down buttons
   work in Cool, Dry and Heat, so `mode_carries_setpoint_()` includes Dry. If
   the captures show a constant temperature nibble in Dry, remove it.
5. **Is there a toggle or sequence bit?** Section A of the capture matrix
   answers this. If pressing the same button twice produces different frames,
   the component needs to alternate that bit, and `on_receive` must mask it out
   before comparing.

## If it turns out not to be a stateful protocol

Everything above assumes each press transmits the complete state, which is the
norm for AC remotes with an LCD, and this remote has one. If the captures show
short, position-independent codes instead, meaning the remote sends "temperature
up" rather than "temperature is 24", then the approach changes:

- The climate entity has to become optimistic: hold an assumed state, and reach
  a requested one by emitting N discrete presses with gaps between them.
- Drift becomes possible whenever someone uses the physical remote or the
  control panel, so a "resync" button that drives the unit to a known state
  (power off, then on into Cool at minimum temperature) becomes necessary.
- `on_receive` can still watch the remote and apply the same increments to the
  assumed state, which limits the drift in practice.

The component is not written this way today because it is strictly worse, and
worth adopting only if the evidence demands it.

## Escape hatch

Any frame you discover but that does not fit the climate entity can be bound to
a button without touching C++:

```yaml
button:
  - platform: template
    name: "Display Brightness"
    on_press:
      - lg_portable_ac.send_frame:
          id: ac
          frame: 0x88C00A6
```

`send_frame` recomputes the checksum by default. Pass
`recalculate_checksum: false` to transmit a captured frame byte for byte.
[`firmware/packages/lg-extras.yaml`](firmware/packages/lg-extras.yaml) uses this
for Light, Swing and the Timer.
