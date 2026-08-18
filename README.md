# ESPHome LG LP0721WSR

Control an **LG LP0721WSR portable air conditioner** from Home Assistant over
infrared, using a [Seeed Studio XIAO Smart IR Mate](https://wiki.seeedstudio.com/xiao_smart_ir_mate/)
as the blaster. You get a real `climate` entity, plus entities for the remote
buttons a climate card has no room for.

Home Assistant's built-in LG integration and ESPHome's `climate_ir_lg` both target
LG's *split-system* remotes, and this unit speaks something completely different —
so neither one works, and no amount of retuning fixes it. This implements the
LP-series protocol instead, measured from the unit's own remote.

**Nothing needs reverse-engineering to use this.** The protocol is already decoded
and built in. Fill in a config, flash, done. The evidence lives in
[`research/`](research/) if you want it.

- [What you get](#what-you-get)
- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Actions](#actions)
- [Troubleshooting](#troubleshooting)
- [How this was built](#how-this-was-built)

## What you get

| Entity | Type | Covers |
| --- | --- | --- |
| Portable AC | `climate` | Power, Cool / Dry / Fan, setpoint 16–30 °C, Low / High fan, swing |
| Display Brightness | `button` | The remote's Light button: On → Dim → Off |
| Delay Timer | `number` | The timer, 0–24 h, where 0 disarms |
| Status LED, Restart, Factory Reset | misc | Device housekeeping |

Three things work better here than on the remote:

**The entity follows the physical remote.** The IR receiver is wired into the
climate component, so if someone picks up the remote, Home Assistant updates
instead of sitting there showing a stale state.

**The timer is absolute.** Setting 12 hours is one transmission. The remote needs
twelve presses and drops out of timer-setting mode if you pause.

**Fahrenheit is exact.** If Home Assistant is set to °F, setpoints are sent in
Fahrenheit, which this protocol carries separately from Celsius and the unit acts
on. That is a finer step than 1 °C, so asking for 78 °F gets you 78 rather than the
79 you would land on by rounding through 26 °C. Nothing to configure — it follows
your Home Assistant units, and pressing the unit button on the physical remote
switches it back.

Swing is a mode on the climate entity rather than a separate button, because the
protocol reports it as a readable state.

### Supported units

Verified on the **LP0721WSR**. Other LP-series portables very likely use the same
frame, since it is the remote family that differs rather than the unit, but nobody
has captured one — if you have another model, [`research/`](research/) has the
procedure and it is about 30 minutes of work.

Heating is deliberately not supported: this unit is cooling only, and the mode byte
the heat-pump variants use has never been captured, so offering it would mean
transmitting a guess. Setting `supports_heat: true` raises a config error rather
than silently doing nothing.

## What you need

- An LG LP0721WSR (or an LP-series portable, with the caveat above)
- A Seeed Studio XIAO Smart IR Mate — an ESP32-C3 with three IR emitters and a receiver
- Home Assistant with the ESPHome integration
- Line of sight from the blaster to the AC's front panel, within about 6 m

The pinout is already configured; it is here for reference only.

| Function | Pin |
| --- | --- |
| IR emitters (3×) | `GPIO3` |
| IR receiver | `GPIO4`, inverted |
| WS2812 status LED | `GPIO7` |
| Vibration motor | `GPIO6` |
| Touch pad | `GPIO5` |
| Reset button | `GPIO9` |

## Quick start

### 1. Get the code

```bash
git clone https://github.com/ambercaravalho/ESPHome-LG-LP0721WSR.git
cd ESPHome-LG-LP0721WSR
```

You will also want the ESPHome CLI, if you do not have it:

```bash
python3 -m venv .venv-esphome && .venv-esphome/bin/pip install esphome
```

### 2. Fill in your secrets

```bash
cp firmware/secrets.yaml.example firmware/secrets.yaml
```

The file explains each value. Two are worth knowing about in advance:

**`device_address`** is how the CLI finds the device for OTA. The configs set
`name_add_mac_suffix: true`, so the device answers to `xiao-ir-mate-<last three MAC
bytes>.local` rather than to `xiao-ir-mate.local`, and the CLI cannot work that
suffix out by itself. Read it off your network with:

```bash
dns-sd -B _esphomelib._tcp        # macOS
avahi-browse -rt _esphomelib._tcp # Linux
```

**`api_encryption_key` and `ota_password`** must match what the device is running
*now* if it is already adopted into Home Assistant, or the OTA push is rejected and
you will have to reflash over USB. Find them in the ESPHome dashboard: open the
device, click Edit. If the device is new, generate fresh values instead
(`openssl rand -base64 32` for the key).

`firmware/secrets.yaml` is gitignored.

### 3. Check the device name

In `firmware/xiao-lg-lp0721wsr.yaml`, the `name` substitution must be
**byte-for-byte** what the device already calls itself, or Home Assistant will
adopt the result as a brand-new device and orphan your existing entities. Seeed's
factory firmware uses `xiao-ir-mate`, which is the default here. New devices can
use anything.

While you are in there, point `room_temperature_entity` at a temperature sensor you
already have in that room, or delete the `sensor:` block. The AC does not report its
own temperature, so this is the only way the climate card can show a current
reading.

### 4. Flash it

```bash
esphome run firmware/xiao-lg-lp0721wsr.yaml
```

The entities appear in Home Assistant automatically. Aim the blaster at the AC and
try the climate card.

### 5. Confirm it is really working

Worth five minutes, because "the entity changed" and "the AC changed" are not the
same thing.

1. **Does the unit respond?** Walk the card through off, on, each mode, both fan
   speeds, and each end of the temperature range. Watch the AC, not Home Assistant.
2. **Do the frames match the remote's?** The log prints `sending` and all fourteen
   bytes on every transmission. Set the same state twice, once from Home Assistant
   and once from the remote, and compare. They should be identical — and if not, the
   byte that differs names the field that is wrong.
3. **Does the remote update Home Assistant?** Change something with the physical
   remote; the entity should follow within a second.

Then set `ir_dump: "none"` and `log_level: "INFO"` to quiet the logs down.

### Using the Home Assistant ESPHome dashboard instead

The bundled configs reference the component by local path, so they work from a
clone. If you would rather paste a config into the dashboard, point
`external_components` at GitHub instead:

```yaml
external_components:
  - source: github://ambercaravalho/ESPHome-LG-LP0721WSR
    components: [lg_portable_ac]
```

You will need to inline the contents of `firmware/packages/` too, since the
dashboard has no local files to `!include`.

## Configuration

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

    # Measured, and already the defaults. Here for reference — you only need
    # these if your own captures disagree.
    header_high: 3150us
    header_low: 1590us
    bit_high: 550us
    bit_one_low: 1070us
    bit_zero_low: 290us
    carrier_frequency: 38000Hz
```

## Actions

```yaml
# Cycle the display brightness, On -> Dim -> Off.
- lg_portable_ac.light_toggle: ac

# Arm the delay timer, or disarm with 0. Absolute, not a counter.
- lg_portable_ac.set_timer:
    id: ac
    hours: 8
```

### Sending arbitrary frames

For anything the component does not model — a fixed vane angle, say — all fourteen
bytes go out as given:

```yaml
button:
  - platform: template
    name: "Vane position 3"
    on_press:
      - lg_portable_ac.send_raw_frame:
          id: ac
          frame: [0x23, 0xCB, 0x26, 0x01, 0x00, 0x24, 0x03, 0x07, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x00]
          recalculate_checksum: true   # false sends a captured frame verbatim
```

A raw frame does not update the climate entity, so the two will disagree until the
next state change. [`research/PROTOCOL.md`](research/PROTOCOL.md) documents what
each byte means.

## Troubleshooting

**The AC ignores everything.** Check line of sight to the sensor on the front
panel, within about 6 m. If the log shows frames going out that match the remote's
byte for byte, the protocol is fine and this is a range or aiming problem.

**`Error resolving IP address` when flashing.** `device_address` in
`secrets.yaml` is wrong or missing. See [step 2](#2-fill-in-your-secrets).

**OTA rejected.** `api_encryption_key` or `ota_password` does not match what the
device is running. Copy them out of the existing dashboard config, or reflash over
USB.

**A second, duplicate device appears in Home Assistant.** The `name` substitution
does not match what the device was previously called. Fix it, reflash, then delete
the stale device from the ESPHome integration page.

**Home Assistant and the AC disagree.** Expected on any IR setup after a power cut,
since IR has no feedback channel. Wiring `receiver_id` in narrows it to cases where
somebody used the control panel on the unit itself.

**Nothing is received at all.** The raw dumper logs at `DEBUG`, so `INFO` hides
every capture. Check `log_level`.

**`checksum mismatch` warnings.** Frames are arriving and being bit-decoded but
failing validation. This usually means receiver saturation rather than a checksum
problem — see the troubleshooting section of
[`research/capture-guide.md`](research/capture-guide.md).

## How this was built

The protocol is **measured, not inherited**. Every field was decoded from 90
captures of this unit's own remote, and those captures are committed so the claims
are checkable rather than taken on trust.

If you want the details:

- [`research/README.md`](research/README.md) — how the protocol was found, and how to repeat it on another remote
- [`research/PROTOCOL.md`](research/PROTOCOL.md) — the frame format, field by field, with the evidence for each claim
- [`research/capture-guide.md`](research/capture-guide.md) — the capture procedure
- [`research/captures/`](research/captures/) — the raw logs
- [`research/tools/`](research/tools/) — the analysis scripts

The short version of why the stock component cannot work: `climate_ir_lg` expects a
28-bit frame behind an 8000/4000 µs header. This remote sends **112 bits** behind a
**3150/1590 µs** header. A decoder looking for the LG protocol does not misread
these frames, it discards them — which looks exactly like "unsupported".

## Repository layout

```
components/lg_portable_ac/    The ESPHome external component
firmware/
  xiao-lg-lp0721wsr.yaml      The firmware you flash
  xiao-ir-capture.yaml        Capture rig, only needed for research
  packages/                   Shared hardware config and the extra entities
  secrets.yaml.example        Template for your credentials
research/                     How the protocol was reverse-engineered
```

## Development

```bash
python3 -m unittest discover -s research/tools -p 'test_*.py'
python3 research/tools/verify_protocol.py
esphome config firmware/xiao-lg-lp0721wsr.yaml
esphome compile firmware/xiao-lg-lp0721wsr.yaml
```

`verify_protocol.py` is the one that matters when touching the protocol. Beyond
checking each documented field, it rebuilds every captured frame from only the
fields the component models and requires a byte-for-byte match — so a field nobody
noticed fails the check, rather than quietly producing transmissions that differ
from the remote's.

## Contributing

Captures from other LG portables are the most useful thing anyone could add.
[`research/capture-guide.md`](research/capture-guide.md) is the procedure; open an
issue or a PR with the raw log and `verify_protocol.py` will say whether your unit
matches this one.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
