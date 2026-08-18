# ESPHome LG LP0721WSR

[![CI](https://github.com/ambercaravalho/ESPHome-LG-LP0721WSR/actions/workflows/ci.yml/badge.svg)](https://github.com/ambercaravalho/ESPHome-LG-LP0721WSR/actions/workflows/ci.yml)
[![Licence: GPL-3.0](https://img.shields.io/badge/licence-GPL--3.0-blue.svg)](LICENSE)

Control an **LG LP0721WSR portable air conditioner** from Home Assistant over
infrared. You get a real `climate` entity, plus entities for the remote buttons a
climate card has no room for.

Home Assistant's built-in LG integration and ESPHome's `climate_ir_lg` both target
LG's *split-system* remotes; this unit speaks something completely different, so
neither works. This implements the LP-series protocol instead, decoded from the
unit's own remote — nothing is left for you to reverse-engineer.

It is a standard [ESPHome external component](https://esphome.io/components/external_components.html),
not a fork of ESPHome, so it updates like any other ESPHome device.

## What you get

| Entity | Type | Covers |
| --- | --- | --- |
| Portable AC | `climate` | Power, Cool / Dry / Fan, setpoint 16–30 °C, Low / High fan, swing |
| Display Brightness | `button` | The remote's Light button: On → Dim → Off |
| Delay Timer | `number` | The timer, 0–24 h, where 0 disarms |

Three things work better here than on the physical remote:

- **The entity follows the remote.** The IR receiver feeds the climate component,
  so using the remote updates Home Assistant instead of leaving it showing a
  stale state.
- **The timer is absolute.** Setting 12 hours is one transmission; the remote
  needs twelve presses and drops out of timer-setting mode if you pause.
- **Fahrenheit is exact.** The protocol carries °F separately from °C, so asking
  for 78 °F gets you 78 rather than the 79 you land on by rounding through 26 °C.

Verified on the **LP0721WSR**; other LP-series portables very likely use the same
frame, but nobody has captured one. Heating is unsupported because the heat-pump
mode byte has never been captured, so `supports_heat: true` raises a config error
rather than transmitting a guess.

## What you need

- An LG LP0721WSR (or an LP-series portable, with the caveat above)
- An ESP32 running ESPHome with an IR emitter, and ideally an IR receiver. The
  reference build is a [Seeed Studio XIAO Smart IR Mate](https://wiki.seeedstudio.com/xiao_smart_ir_mate/);
  its pinout is in [`firmware/packages/xiao-hardware.yaml`](firmware/packages/xiao-hardware.yaml).
- Home Assistant with the ESPHome integration
- Line of sight from the blaster to the AC's front panel, within about 6 m

## Add it to a device you already have

If you already run an IR blaster, this is the whole job. The AC arrives as a few
extra entities and touches nothing else on the device — and because a
`remote_transmitter` accepts any number of consumers, that blaster keeps driving
your TV or anything else.

Add two blocks to your existing config:

```yaml
external_components:
  - source: github://ambercaravalho/ESPHome-LG-LP0721WSR
    components: [lg_portable_ac]

packages:
  lg_ac: github://ambercaravalho/ESPHome-LG-LP0721WSR/firmware/packages/lg-portable-ac.yaml@main
```

Then point it at your hardware with a `substitutions:` block:

| Substitution | Default | Purpose |
| --- | --- | --- |
| `lg_ac_transmitter_id` | `ir_tx` | The `remote_transmitter` to send on |
| `lg_ac_receiver_id` | `ir_rx` | The `remote_receiver` that syncs remote presses |
| `lg_ac_id` | `ac` | Id of the climate entity, for use in your automations |
| `lg_ac_name` | `Portable AC` | Entity name; `None` inherits the device's friendly name |
| `lg_ac_temperature_entity` | `sensor.living_room_temperature` | Supplies "current temperature", which the AC does not report |
| `lg_ac_supports_swing` | `"true"` | Set `"false"` to hide the control on a fixed-vane unit |

To take the temperature from a thermostat rather than a bare sensor, add the
attribute that carries the reading — a climate entity's state is its HVAC mode,
not a number:

```yaml
sensor:
  - id: !extend lg_ac_room_temperature
    attribute: current_temperature
```

[`firmware/example-generic-blaster.yaml`](firmware/example-generic-blaster.yaml)
is a complete worked example on non-XIAO hardware, with a TV button sharing the
same emitter. Copy its `remote_receiver` settings too: the ESPHome defaults
truncate a frame this long.

## Or flash a dedicated device

For a XIAO Smart IR Mate doing nothing but the AC:

```bash
git clone https://github.com/ambercaravalho/ESPHome-LG-LP0721WSR.git
cd ESPHome-LG-LP0721WSR
cp firmware/secrets.yaml.example firmware/secrets.yaml
```

Fill in `firmware/secrets.yaml` (it explains each value; it is gitignored), set
`lg_ac_temperature_entity` in
[`firmware/xiao-lg-lp0721wsr.yaml`](firmware/xiao-lg-lp0721wsr.yaml), then flash:

```bash
esphome run firmware/xiao-lg-lp0721wsr.yaml
```

If the device is **already adopted into Home Assistant**, the `name` substitution
and the `api_encryption_key` / `ota_password` secrets must match what it is running
now, or you will orphan its entities and have to reflash over USB. All three are in
the ESPHome dashboard under Edit; Seeed's factory firmware uses `xiao-ir-mate`. A
new device can use anything (`openssl rand -base64 32`).

Entities appear automatically. Test by watching the AC rather than Home Assistant:
the log prints all fourteen bytes of every transmission, so if the unit ignores
something, set the same state from the physical remote and compare frames. Once
happy, set `ir_dump: "none"` and `log_level: "INFO"`.

## Configuration

Declaring the climate entity yourself, instead of using the package:

```yaml
climate:
  - platform: lg_portable_ac
    id: ac
    transmitter_id: ir_tx
    receiver_id: ir_rx          # optional, syncs presses from the physical remote
    sensor: room_temperature    # optional, provides "current temperature"
    supports_swing: true
```

Protocol timings (`header_high`, `bit_one_low` and friends) are measured and
already the defaults; set them only if your own captures disagree.

## Actions

```yaml
# Cycle the display brightness, On -> Dim -> Off.
- lg_portable_ac.light_toggle: ac

# Arm the delay timer, or disarm with 0. Absolute, not a counter.
- lg_portable_ac.set_timer:
    id: ac
    hours: 8

# Anything the component does not model, a fixed vane angle say. All fourteen
# bytes go out as given, and the climate entity will not track the result.
- lg_portable_ac.send_raw_frame:
    id: ac
    frame: [0x23, 0xCB, 0x26, 0x01, 0x00, 0x24, 0x03, 0x07, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x00]
    recalculate_checksum: true   # false sends a captured frame verbatim
```

[`research/PROTOCOL.md`](research/PROTOCOL.md) documents what each byte means.

## Troubleshooting

**The AC ignores everything.** Check line of sight to the sensor on the front
panel, within about 6 m. If the log shows frames matching the remote's byte for
byte, the protocol is fine and this is a range or aiming problem.

**Nothing is received, or `checksum mismatch` warnings.** Usually receiver
settings: an LG frame is 112 bits and the ESPHome defaults truncate it, so copy the
`remote_receiver` block from the example above. Note the raw dumper logs at
`DEBUG`, so `INFO` hides every capture.

**OTA rejected, or `Error resolving IP address`.** `device_address`,
`api_encryption_key` or `ota_password` does not match what the device is running.
Copy them from the dashboard config, or reflash over USB.

**A second, duplicate device appears in Home Assistant.** The `name` substitution
does not match what the device was previously called. Fix it, reflash, then delete
the stale device from the ESPHome integration page.

**Home Assistant and the AC disagree.** Expected on any IR setup after a power cut,
since IR has no feedback channel. Setting `lg_ac_receiver_id` narrows it to cases
where somebody used the control panel on the unit itself.

## How this was built

Every field was decoded from 90 captures of this unit's own remote, and those
captures are committed so the claims are checkable. Why the stock component cannot
work: `climate_ir_lg` expects a 28-bit frame behind an 8000/4000 µs header, while
this remote sends **112 bits** behind a **3150/1590 µs** header. A decoder looking
for the LG protocol does not misread these frames, it discards them — which looks
exactly like "unsupported".

[`research/`](research/) has the full story: the
[frame format](research/PROTOCOL.md) field by field, the
[capture procedure](research/capture-guide.md), the raw logs and the analysis
scripts. Captures from other LG portables are the most useful contribution anyone
could make — open an issue or PR with the raw log.

## Development

```bash
python3 -m unittest discover -s research/tools -p 'test_*.py'
python3 research/tools/verify_protocol.py
esphome config firmware/xiao-lg-lp0721wsr.yaml
esphome config firmware/example-generic-blaster.yaml
```

`verify_protocol.py` rebuilds every captured frame from only the fields the
component models and requires a byte-for-byte match, so a field nobody noticed
fails the check rather than quietly producing wrong transmissions. CI runs this
weekly as well as on every push, so an ESPHome release that breaks the component
shows up before you flash.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
