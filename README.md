# ESPHome-LG-LP0721WSR

Home Assistant (ESPHome) infrared control for the LG LP0721WSR portable air
conditioner, using a Seeed Studio XIAO Smart IR Mate as the blaster.

Home Assistant's built-in LG climate integration and ESPHome's `climate_ir_lg`
both target LG's split-system remotes. This gives you a real `climate` entity
for the LP-series portables instead, plus entities for the remote buttons that a
climate entity has no room for.

## What you get

| Entity | Type | Covers |
| --- | --- | --- |
| Portable AC | `climate` | Power, Cool / Dry / Fan modes, setpoint 16-30 °C, Low / High fan, swing |
| Display Brightness | `button` | Light button: On → Dim → Off |
| Swing Toggle | `button` | Louvre swing on models that have it |
| Delay Timer | `number` | Timer, 1-24 h |
| Status LED, Vibration, Capture Label | misc | Feedback and capture helpers |

Because the receiver is wired into the climate component, picking up the physical
remote updates Home Assistant too, instead of leaving the entity showing a stale
state.

## Status: read this before you start

**The IR frame format implemented here is not yet confirmed against an actual
LP0721WSR.** It is the documented LG frame layout, which is validated against LG
split systems, and this repo reproduces three frames captured from real LG
hardware exactly, checksum included. But no one has published a decode for the
`COV36174376` portable remote, so whether the LP-series uses the same layout is
an open question that only a capture from your unit can answer.

Everything needed to answer it is here and tested: a capture firmware, a
documented capture procedure, and a decoder that prints a drop-in replacement for
the protocol table. See [PROTOCOL.md](PROTOCOL.md) for exactly what is confirmed
and what is not.

### Try the boring fix first

ESPHome's `climate_ir_lg` defaults to an 8000 µs / 4000 µs header. Many LG
remotes, portables included, use roughly 3200 µs / 9900 µs. Wrong header timings
do not cause an error, the unit just ignores you, which looks identical to
"unsupported protocol". So before anything else:

```yaml
climate:
  - platform: climate_ir_lg
    name: "Portable AC"
    transmitter_id: ir_tx
    receiver_id: ir_rx
    header_high: 3200us
    header_low: 9900us
```

If the AC responds, you are done and you do not need this component.

## Hardware

Seeed Studio XIAO Smart IR Mate (XIAO ESP32-C3):

| Function | Pin |
| --- | --- |
| IR emitters (3x) | `GPIO3` |
| IR receiver | `GPIO4`, inverted |
| WS2812 status LED | `GPIO7` |
| Vibration motor | `GPIO6` |
| Touch pad | `GPIO5` |
| Reset button | `GPIO9` |

## Setup

### 1. Secrets

The device is presumably already adopted into Home Assistant, so reuse its
existing credentials or the OTA push will be rejected and you will have to
reflash over USB.

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
```

Fill in your Wi-Fi details, then copy `api.encryption.key` and `ota.password`
out of the config the device is running now (ESPHome dashboard → your device →
Edit) into `secrets.yaml`. `firmware/secrets.yaml` is gitignored.

### 2. Confirm the toolchain

No dependencies beyond the Python standard library. A synthetic session is
checked in so you can prove the analysis pipeline works before touching
hardware:

```bash
python3 tools/decode.py tools/fixtures/example-session.txt --emit-cpp
python3 -m unittest discover -s tools -p 'test_*.py'
```

### 3. Capture your remote

```bash
esphome run firmware/xiao-ir-capture.yaml
```

Then follow [captures/README.md](captures/README.md). It is a specific sequence
of button presses, in a specific order, for a reason: the decoder locates a field
by comparing two captures that differ in exactly one thing. Budget 30 minutes.

Save the logs into `captures/` and analyse them:

```bash
python3 tools/decode.py captures/ --emit-cpp
```

### 4. Reconcile

Compare the decoder's output against the `PROTOCOL TABLE` block in
[`components/lg_portable_ac/lg_portable_ac.h`](components/lg_portable_ac/lg_portable_ac.h).

- **Timings differ** → change them in `firmware/xiao-lg-lp0721wsr.yaml`. No
  recompile of the component needed, they are config options.
- **Frame length, chunking, or checksum differ** → also just YAML
  (`frame_bits`, `chunk_bits`, `chunk_gap_low`, `checksum`).
- **Field positions or command values differ** → paste the generated block over
  the `PROTOCOL TABLE` in the header.

### 5. Flash the real firmware

Set `room_temperature_entity` to a temperature sensor you already have in that
room (the AC does not report its own), then:

```bash
esphome run firmware/xiao-lg-lp0721wsr.yaml
```

## Using it from the Home Assistant ESPHome dashboard

The bundled configs reference the component by local path so they work from a
clone. If you are pasting into the dashboard instead, swap the
`external_components` block for:

```yaml
external_components:
  - source: github://ambercaravalho/ESPHome-LG-LP0721WSR
    components: [lg_portable_ac]
```

## Component configuration

```yaml
climate:
  - platform: lg_portable_ac
    id: ac
    name: "Portable AC"
    transmitter_id: ir_tx
    receiver_id: ir_rx          # optional, but this is what syncs remote presses
    sensor: room_temperature    # optional, provides "current temperature"

    supports_heat: false        # true on heat-pump models (LP0721SHR)
    supports_swing: false       # true only if the unit has motorised louvres

    frame_bits: 28              # 8-64
    checksum: nibble_sum        # none | nibble_sum | byte_sum | nibble_xor | byte_xor
    header_high: 3200us
    header_low: 9900us
    bit_high: 500us
    bit_one_low: 1600us
    bit_zero_low: 550us
    chunk_bits: 0               # >0 if the frame is split by a mid-frame gap
    chunk_gap_low: 8000us
    carrier_frequency: 38000Hz
```

### Sending arbitrary frames

Any frame you discover that does not map onto the climate entity can be bound to
a button with no recompile:

```yaml
button:
  - platform: template
    name: "Display Brightness"
    on_press:
      - lg_portable_ac.send_frame:
          id: ac
          frame: 0x88C00A6
          recalculate_checksum: true   # false to send a captured frame verbatim
```

## Verifying on hardware

Once flashed, with `ir_dump: "raw"` still set:

1. **Does it respond?** Walk the climate card through off, on, each mode, both
   fan speeds, and the ends of the temperature range. Watch the unit, not just
   Home Assistant.
2. **Do our frames match the remote's?** The log prints `sending 0x...` for
   every transmission and `received 0x...` for everything the receiver hears,
   including our own output. Set the same state twice, once from Home Assistant
   and once from the remote, and compare the two hex values. They should be
   identical. If they differ, the differing nibble tells you which field is
   wrong.
3. **Does the remote update Home Assistant?** Change something with the physical
   remote and confirm the entity follows within a second.
4. **Watch for `checksum mismatch`** warnings in the log. Those mean the frames
   are being decoded but the checksum hypothesis is wrong.

Set `ir_dump: "none"` and `log_level: "INFO"` when you are happy.

## Troubleshooting

**The AC ignores everything.** Almost always the header timings. Capture the
remote and compare. Also confirm the emitters have line of sight to the sensor on
the front panel of the unit, within about 6 m.

**Frames arrive with a varying symbol count.** They are being split or
truncated. Raise `idle` in `firmware/packages/xiao-hardware.yaml` (capped at
65534 µs at `clock_resolution: "500000"`), and raise `rmt_symbols` from the
ESP32-C3 default of 96 to 192 plus `buffer_size: 20000b` if it is truncation.
Note that `memory_blocks` no longer exists in current ESPHome; `rmt_symbols` is
its replacement.

**Nothing is received at all.** The raw dumper logs at `DEBUG`, so `INFO` hides
every capture. Check `log_level`.

**Home Assistant and the AC disagree.** Expected on any IR-only setup after a
power cut, since there is no feedback channel. Wiring `receiver_id` in limits it
to cases where nobody touched the control panel directly.

**OTA rejected after switching configs.** `api.encryption.key` or `ota.password`
in `secrets.yaml` does not match what the device is running. Copy them from the
existing dashboard config, or reflash over USB.

## Repository layout

```
components/lg_portable_ac/    ESPHome external component (climate platform)
firmware/
  xiao-ir-capture.yaml        Phase 1: capture rig
  xiao-lg-lp0721wsr.yaml      Final firmware
  packages/
    xiao-hardware.yaml        Board, radio, IR, LED, haptics (shared)
    lg-extras.yaml            Light, Swing, Timer entities
captures/                     Your logs, plus the capture procedure
tools/
  parse_raw.py                ESPHome log -> raw frames
  decode.py                   Raw frames -> timings, fields, checksum, C++ table
  test_decode.py              Tests for both
  fixtures/                   Synthetic session for validating the toolchain
PROTOCOL.md                   Frame format: what is confirmed, what is not
```

## Development

```bash
python3 -m unittest discover -s tools -p 'test_*.py'
esphome config firmware/xiao-lg-lp0721wsr.yaml
esphome compile firmware/xiao-lg-lp0721wsr.yaml
```

The test suite pins the protocol table against frames captured from real LG
hardware, so changing a field shift, a command value, or the checksum without
updating the tests will fail.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
