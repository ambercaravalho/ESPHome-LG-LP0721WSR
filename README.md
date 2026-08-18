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
| Delay Timer | `number` | Timer, 0-24 h, where 0 disarms |
| Status LED, Vibration, Capture Label | misc | Feedback and capture helpers |

Because the receiver is wired into the climate component, picking up the physical
remote updates Home Assistant too, instead of leaving the entity showing a stale
state. Swing has its own control on the climate entity rather than a separate
button, because the protocol reports it as a state that can be read back.

The Delay Timer is absolute: setting 12 hours is one transmission, where the remote
needs twelve presses and drops out of timer-setting mode if you pause. Both are
easier from Home Assistant than from the remote.

## Status

**The protocol is measured, not inherited.** Every field is decoded from 90
captures of this unit's own remote, committed to
[`captures/`](captures/) so the claims are reproducible.
[`tools/verify_protocol.py`](tools/verify_protocol.py) re-checks
[PROTOCOL.md](PROTOCOL.md) against them, including a round-trip test that rebuilds
each frame from only the fields the component models — so a field we had missed
would fail the build rather than quietly produce wrong transmissions.

What remains untested is listed at the end of PROTOCOL.md, and all of it concerns
frames the remote physically cannot produce, such as fixed vane positions and
half-hour timer steps.

### Why the stock component cannot work

Worth stating plainly, because "just fix the header timings" is the usual advice
for an LG remote that does not respond, and here it cannot help. `climate_ir_lg`
implements a 28-bit frame with an 8000/4000 µs header, and tuning it to the
3200/9900 µs some LG remotes use is the standard next step.

This remote is neither. It sends **112 bits** behind a **3150/1590 µs** header:
four times the frame length, with a header space off by a factor of six. A receiver
looking for the LG protocol does not misdecode these frames, it discards them —
which is indistinguishable from "unsupported" and is why no amount of configuration
fixes it.

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

### 2. Match your device's name and address

Both firmware configs set `name_add_mac_suffix: true`, so the device answers to
`${name}-<last three MAC bytes>` rather than to `${name}`. Two substitutions at
the top of `firmware/xiao-ir-capture.yaml` and `firmware/xiao-lg-lp0721wsr.yaml`
have to agree with reality:

- `name` must be byte-for-byte the name the device already uses, or Home
  Assistant will treat the result as a brand-new device and orphan the existing
  entities. Seeed's factory firmware uses `xiao-ir-mate`.
- `device_address` is how the `esphome` CLI finds the device for OTA. The CLI
  cannot derive the MAC suffix by itself, so without this it tries the
  suffix-less `${name}.local` and fails to resolve.

To read both off the network, browse for the ESPHome mDNS service:

```bash
dns-sd -B _esphomelib._tcp
```

The advertised instance name is the full hostname. Strip the trailing
`-<six hex digits>` to get `name`, and append `.local` to get `device_address`.
An IP address works for `device_address` too, at the cost of breaking whenever
DHCP reassigns it.

### 3. Confirm the toolchain

No dependencies beyond the Python standard library. A synthetic session is
checked in so you can prove the analysis pipeline works before touching
hardware:

```bash
python3 tools/decode.py tools/fixtures/example-session.txt --layout
python3 -m unittest discover -s tools -p 'test_*.py'
```

### 4. Capture your remote

```bash
esphome run firmware/xiao-ir-capture.yaml
```

Then follow [captures/README.md](captures/README.md). It is a specific sequence
of button presses, in a specific order, for a reason: the decoder locates a field
by comparing two captures that differ in exactly one thing. Budget 30 minutes.

Save the logs into `captures/` and analyse them:

```bash
python3 tools/decode.py captures/ --layout
```

### 5. Reconcile

Only needed if your remote turns out to differ from the one this was measured
from. Check your captures against the documented layout:

```bash
python3 tools/verify_protocol.py captures/
```

Silence means your remote matches and there is nothing to do. Otherwise:

- **Timings differ** → change them in `firmware/xiao-lg-lp0721wsr.yaml`. They are
  config options, so the component does not need recompiling.
- **Field positions or values differ** → edit the `PROTOCOL TABLE` block in
  [`components/lg_portable_ac/lg_portable_ac.h`](components/lg_portable_ac/lg_portable_ac.h),
  which is a small set of named constants rather than a bit-field table.

### 6. Flash the real firmware

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

    # Set false on units with a fixed vane, purely to hide the control.
    supports_swing: true

    # All measured, and all already the defaults. Listed for reference; you only
    # need them if your own captures disagree.
    header_high: 3150us
    header_low: 1590us
    bit_high: 550us
    bit_one_low: 1070us
    bit_zero_low: 290us
    carrier_frequency: 38000Hz
```

There is no `supports_heat`: the LP0721WSR is cooling only, and the mode byte
heat-pump variants use has never been captured, so enabling it would transmit a
guess. Setting it raises a config error rather than silently doing nothing.

### Actions

```yaml
# Cycle the display brightness, On -> Dim -> Off.
- lg_portable_ac.light_toggle: ac

# Arm the delay timer, or disarm it with 0. Absolute, not a counter.
- lg_portable_ac.set_timer:
    id: ac
    hours: 8
```

### Sending arbitrary frames

For frames the component does not model — a fixed vane position, a Fahrenheit
setpoint — all fourteen bytes go out as given:

```yaml
button:
  - platform: template
    name: "Vane position 3"
    on_press:
      - lg_portable_ac.send_raw_frame:
          id: ac
          frame: [0x23, 0xCB, 0x26, 0x01, 0x00, 0x24, 0x03, 0x07, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x00]
          recalculate_checksum: true   # false to send a captured frame verbatim
```

A raw frame does not update the climate entity, so it will disagree with the unit
until the next state change.

## Verifying on hardware

Once flashed, with `ir_dump: "raw"` still set:

1. **Does it respond?** Walk the climate card through off, on, each mode, both
   fan speeds, and the ends of the temperature range. Watch the unit, not just
   Home Assistant.
2. **Do our frames match the remote's?** The log prints `sending` followed by all
   fourteen bytes for every transmission. Set the same state twice, once from Home
   Assistant and once from the remote, and compare. They should be byte-identical,
   and the byte that differs names the field that is wrong.
3. **Does the remote update Home Assistant?** Change something with the physical
   remote and confirm the entity follows within a second.
4. **Watch for `checksum mismatch`** warnings. Those mean frames are being received
   and bit-decoded but failing validation, which usually points at receiver
   saturation rather than the checksum itself; see `captures/README.md`.
5. **Check the timer both ways.** Set it from Home Assistant and confirm the unit's
   display agrees, then set it from the remote and confirm the number entity
   follows. It reads back from received frames, so this exercises both directions.

Set `ir_dump: "none"` and `log_level: "INFO"` when you are happy.

## Troubleshooting

**The AC ignores everything.** Confirm the emitters have line of sight to the
sensor on the front panel, within about 6 m. If the log shows frames going out and
they match what the remote sends byte for byte, the protocol is not the problem and
it is a range or aiming issue.

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

**`Error resolving IP address` on upload.** The CLI is looking for
`${name}.local`, but `name_add_mac_suffix` means the device answers to
`${name}-<mac suffix>`. Set the `device_address` substitution, per setup step 2.

**A second, duplicate device appears in Home Assistant.** The `name`
substitution does not match what the device was previously called. Fix `name`,
reflash, then delete the stale device from the ESPHome integration page.

## Repository layout

```
components/lg_portable_ac/    ESPHome external component (climate platform)
firmware/
  xiao-ir-capture.yaml        Phase 1: capture rig
  xiao-lg-lp0721wsr.yaml      Final firmware
  packages/
    xiao-hardware.yaml        Board, radio, IR, LED, haptics (shared)
    lg-extras.yaml            Light button and Delay Timer entities
captures/                     Your logs, plus the capture procedure
tools/
  parse_raw.py                ESPHome log -> raw frames
  decode.py                   Raw frames -> timings, fields, checksum, byte layout
  verify_protocol.py          Asserts PROTOCOL.md against the captures
  test_decode.py              Tests for the tooling
  fixtures/                   Synthetic session for validating the toolchain
PROTOCOL.md                   Frame format, and the evidence for each field
```

## Development

```bash
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/verify_protocol.py
esphome config firmware/xiao-lg-lp0721wsr.yaml
esphome compile firmware/xiao-lg-lp0721wsr.yaml
```

`verify_protocol.py` is the one that matters when touching the protocol. Beyond
checking each field, it rebuilds every captured frame from only the fields the
component models and requires a byte-for-byte match, so a field nobody noticed
fails the check rather than quietly producing transmissions that differ from the
remote's.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
