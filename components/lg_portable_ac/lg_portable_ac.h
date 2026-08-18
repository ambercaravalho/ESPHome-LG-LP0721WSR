#pragma once

#include <array>
#include <cinttypes>

#include "esphome/components/climate_ir/climate_ir.h"
#include "esphome/core/automation.h"
#include "esphome/core/helpers.h"

namespace esphome {
namespace lg_portable_ac {

// ===========================================================================
// PROTOCOL TABLE
// ===========================================================================
// Measured from 86 captures of the original LP0721WSR remote. Every value here
// is observed, not inherited from another LG model, and PROTOCOL.md records the
// evidence for each one. tools/verify_protocol.py re-checks these against the
// committed captures.
//
// A frame is 14 bytes sent least significant bit first:
//
//   byte   0  1  2  3  4   5     6     7     8     9    10 11 12   13
//         23 CB 26 01 00  flg  mode  temp  fan+  timer  00 00 00  cksum
//
// The protocol is fully stateful: every frame carries the complete state, so
// there are no incremental or toggle commands to track. Light is the sole
// exception, being a press the unit acts on rather than a state it stores.
// ---------------------------------------------------------------------------

static const uint8_t FRAME_BYTES = 14;
static const uint8_t FRAME_BITS = FRAME_BYTES * 8;

/// Byte offsets of the fields that carry state.
enum FrameIndex : uint8_t {
  IDX_FLAGS = 5,
  IDX_MODE = 6,
  IDX_TEMPERATURE = 7,
  IDX_FAN = 8,
  IDX_TIMER = 9,
  IDX_FAHRENHEIT = 12,
  IDX_CHECKSUM = 13,
};

/// Bytes 0-4, identical in every frame ever captured. Used to reject frames
/// from other remotes before trusting anything else in them.
static const uint8_t PREFIX_BYTES = 5;
static const uint8_t PREFIX[PREFIX_BYTES] = {0x23, 0xCB, 0x26, 0x01, 0x00};

// Byte 5. A flag byte, not a command: power, mode and timer are independent, so
// an off frame keeps the mode, temperature and fan of the state it was in.
static const uint8_t FLAGS_BASE = 0x20;   ///< Set in every captured frame.
static const uint8_t FLAG_POWER = 0x04;   ///< Clear means off.
static const uint8_t FLAG_TIMER = 0x08;   ///< Set while a timer is armed.
static const uint8_t FLAG_LIGHT = 0x40;   ///< Marks a Light press; see below.

// Byte 6. The Mode button cycles these three; the LP0721WSR has no auto mode
// and no heat. Heat exists on SHR variants but its value is unknown, so this
// component does not offer it rather than guessing.
static const uint8_t MODE_COOL = 0x03;
static const uint8_t MODE_DRY = 0x02;
static const uint8_t MODE_FAN_ONLY = 0x07;

// Byte 7. The setpoint counts *down*: 16C is 0x0F and 30C is 0x01.
static const uint8_t TEMPERATURE_BASE = 31;
static const float TEMPERATURE_MIN_C = 16.0f;
static const float TEMPERATURE_MAX_C = 30.0f;
static const float TEMPERATURE_STEP_C = 1.0f;

// Byte 8 is packed rather than being a plain fan byte.
static const uint8_t FAN_MASK = 0x07;
static const uint8_t FAN_LOW = 0x02;
static const uint8_t FAN_HIGH = 0x05;
static const uint8_t SWING_MASK = 0x38;
static const uint8_t SWING_SHIFT = 3;
static const uint8_t SWING_OFF_VALUE = 0x00;
static const uint8_t SWING_ON_VALUE = 0x07;
/// Set on frames the remote sends while its timer-setting mode is open. It is
/// the remote's own UI state and clears when that mode times out, so we never
/// set it ourselves. Masked off when decoding so it cannot be read as fan or
/// swing data.
static const uint8_t FAN_BYTE_REMOTE_TIMER_MODE = 0x40;

// Byte 9 counts ten-minute units, so an hour is 6. The remote only ever emits
// whole hours; finer values are accepted by this component but untested against
// the hardware.
static const uint8_t TIMER_UNITS_PER_HOUR = 6;
static const uint8_t TIMER_MAX_HOURS = 24;
static const uint8_t TIMER_OFF = 0;

// Byte 12 is zero while the remote shows Celsius and 0x80 | degrees Fahrenheit
// once its display is switched over. Byte 7 carries the rounded Celsius setpoint
// either way, so the two always agree but are not redundant: 1F is a finer step
// than 1C, so Fahrenheit is the higher-resolution way to state a setpoint.
static const uint8_t FAHRENHEIT_FLAG = 0x80;
static const uint8_t FAHRENHEIT_MASK = 0x7F;
/// How far from a whole degree Celsius a request has to be before we take it as
/// evidence of a Fahrenheit UI. Half a degree F is ~0.28C, so this sits well inside
/// that while ignoring float noise.
static const float WHOLE_CELSIUS_EPSILON = 0.05f;

// ===========================================================================

/// One complete frame, checksum included.
using Frame = std::array<uint8_t, FRAME_BYTES>;

class LgPortableAcClimate : public climate_ir::ClimateIR {
 public:
  LgPortableAcClimate()
      : climate_ir::ClimateIR(TEMPERATURE_MIN_C, TEMPERATURE_MAX_C, TEMPERATURE_STEP_C,
                              /* supports_dry */ true, /* supports_fan_only */ true,
                              {climate::CLIMATE_FAN_LOW, climate::CLIMATE_FAN_HIGH},
                              {climate::CLIMATE_SWING_OFF, climate::CLIMATE_SWING_VERTICAL}) {
    // ClimateIR defaults this on, and it is not a config option here: the mode
    // byte for heat is unknown, so offering the mode would mean transmitting a
    // guess. SHR owners need a capture first.
    this->supports_heat_ = false;
  }

  void set_header_high(uint32_t value) { this->header_high_ = value; }
  void set_header_low(uint32_t value) { this->header_low_ = value; }
  void set_bit_high(uint32_t value) { this->bit_high_ = value; }
  void set_bit_one_low(uint32_t value) { this->bit_one_low_ = value; }
  void set_bit_zero_low(uint32_t value) { this->bit_zero_low_ = value; }
  void set_supports_swing(bool value) { this->supports_swing_ = value; }
  void set_carrier_frequency(uint32_t value) { this->carrier_frequency_ = value; }

  void dump_config() override;

  /** Cycle the display brightness: On -> Dim -> Off, one step per press.
   *
   * The unit walks the cycle internally and the frame carries no brightness
   * value, so there is nothing to read back and no way to ask for a specific
   * level. That makes this a button, not a select.
   */
  void send_light_toggle();

  /** Arm the delay timer, or disarm it with zero hours.
   *
   * Absolute, not a counter: the frame states the whole value, so setting 12
   * hours is one transmission rather than twelve button presses.
   */
  void set_timer_hours(uint8_t hours);
  uint8_t get_timer_hours() const { return this->timer_hours_; }

  /** Transmit an arbitrary 14-byte frame.
   *
   * An escape hatch for frames this component does not model, such as the fixed
   * vane positions the swing field looks wide enough to hold. The checksum is
   * recomputed unless you ask for the bytes to go out untouched.
   */
  void send_raw_frame(const std::vector<uint8_t> &bytes, bool recalculate_checksum);

 protected:
  climate::ClimateTraits traits() override;
  void transmit_state() override;
  bool on_receive(remote_base::RemoteReceiveData data) override;

  /// Build the frame for the current entity state, checksum included.
  Frame encode_state_();
  /// Apply a received frame to the entity state. False if it is not ours.
  bool decode_frame_(const Frame &frame);
  /// Put the pulse train for `frame` on the wire.
  void transmit_frame_(const Frame &frame);

  static uint8_t checksum_(const Frame &frame);
  static bool prefix_valid_(const Frame &frame);

  uint8_t mode_byte_() const;
  uint8_t fan_byte_() const;

  uint32_t header_high_{3150};
  uint32_t header_low_{1590};
  uint32_t bit_high_{550};
  uint32_t bit_one_low_{1070};
  uint32_t bit_zero_low_{290};
  uint32_t carrier_frequency_{38000};
  bool supports_swing_{true};

  /// Hours remaining on the delay timer, 0 when disarmed. Part of every state
  /// frame, so it lives here rather than in whatever entity exposes it.
  uint8_t timer_hours_{TIMER_OFF};

  /// An off frame keeps the mode byte of the state the unit was in, so powering
  /// off does not tell us to forget the mode. Tracked to reproduce that.
  climate::ClimateMode last_active_mode_{climate::CLIMATE_MODE_COOL};

  /// Whether to state the setpoint in Fahrenheit as well (byte 12). Latched rather
  /// than decided per frame, because 77F is exactly 25C: judging each request on
  /// its own would silently drop back to Celsius at that one value and flip the
  /// unit's display back and forth as the setpoint moved past it.
  bool fahrenheit_{false};
};

template<typename... Ts> class SendRawFrameAction : public Action<Ts...>, public Parented<LgPortableAcClimate> {
 public:
  TEMPLATABLE_VALUE(std::vector<uint8_t>, frame)
  TEMPLATABLE_VALUE(bool, recalculate_checksum)

  void play(const Ts &...x) override {
    this->parent_->send_raw_frame(this->frame_.value(x...), this->recalculate_checksum_.value(x...));
  }
};

template<typename... Ts> class SetTimerAction : public Action<Ts...>, public Parented<LgPortableAcClimate> {
 public:
  TEMPLATABLE_VALUE(uint8_t, hours)

  void play(const Ts &...x) override { this->parent_->set_timer_hours(this->hours_.value(x...)); }
};

template<typename... Ts> class LightToggleAction : public Action<Ts...>, public Parented<LgPortableAcClimate> {
 public:
  void play(const Ts &...x) override { this->parent_->send_light_toggle(); }
};

}  // namespace lg_portable_ac
}  // namespace esphome
