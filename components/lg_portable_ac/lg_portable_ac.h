#pragma once

#include <cinttypes>

#include "esphome/components/climate_ir/climate_ir.h"
#include "esphome/core/automation.h"
#include "esphome/core/helpers.h"

namespace esphome {
namespace lg_portable_ac {

/// Algorithm used to compute the trailing checksum field.
enum ChecksumType : uint8_t {
  CHECKSUM_NONE = 0,
  CHECKSUM_NIBBLE_SUM,
  CHECKSUM_BYTE_SUM,
  CHECKSUM_NIBBLE_XOR,
  CHECKSUM_BYTE_XOR,
};

/// A field inside the frame: `width` bits whose least significant bit sits at
/// `shift`, counting from the *last* transmitted bit. Measuring from the end
/// rather than the start means the table below stays valid if the frame turns
/// out to be longer than the default, because extra bytes are prepended.
struct BitField {
  uint8_t shift;
  uint8_t width;

  uint64_t value_mask() const { return (1ULL << this->width) - 1ULL; }
  uint64_t frame_mask() const { return this->value_mask() << this->shift; }
  uint64_t get(uint64_t frame) const { return (frame >> this->shift) & this->value_mask(); }
  void set(uint64_t &frame, uint64_t value) const {
    frame = (frame & ~this->frame_mask()) | ((value & this->value_mask()) << this->shift);
  }
};

// ===========================================================================
// PROTOCOL TABLE
// ===========================================================================
// This is the documented LG air-conditioner frame layout: an 0x88 signature, a
// command byte that folds the power state together with the operating mode, a
// temperature nibble, a fan nibble, and a checksum nibble.
//
// CONFIRMED for LG split systems (this is what ESPHome's built-in
// `climate_ir_lg` implements, and it is field-tested against real hardware).
//
// NOT YET CONFIRMED for the LP-series portables. If your captures disagree,
// this block plus the timings in `climate.py` are the only things that need to
// change. `python3 tools/decode.py captures/ --emit-cpp` prints a replacement
// for this block directly. See PROTOCOL.md for the reasoning and the current
// state of the evidence.
// ---------------------------------------------------------------------------

static const uint8_t SIGNATURE_WIDTH = 8;
static const uint64_t SIGNATURE = 0x88;

static const BitField FIELD_COMMAND = {12, 8};
static const BitField FIELD_TEMPERATURE = {8, 4};
static const BitField FIELD_FAN = {4, 4};
static const BitField FIELD_CHECKSUM = {0, 4};

// Command byte. LG distinguishes "switch on into mode X" from "change to mode X
// while already running", so the same requested mode produces a different frame
// depending on what the unit was doing before.
static const uint8_t CMD_OFF = 0xC0;
static const uint8_t CMD_SWING = 0x10;

static const uint8_t CMD_ON_COOL = 0x00;
static const uint8_t CMD_ON_DRY = 0x01;
static const uint8_t CMD_ON_FAN_ONLY = 0x02;
static const uint8_t CMD_ON_HEAT = 0x04;

static const uint8_t CMD_COOL = 0x08;
static const uint8_t CMD_DRY = 0x09;
static const uint8_t CMD_FAN_ONLY = 0x0A;
static const uint8_t CMD_HEAT = 0x0C;

// Fan nibble. The LP-series has only two speeds, so MEDIUM and AUTO are listed
// for decoding frames from other LG remotes but are never transmitted.
static const uint8_t FAN_LOW = 0x0;
static const uint8_t FAN_MEDIUM = 0x2;
static const uint8_t FAN_HIGH = 0x4;
static const uint8_t FAN_AUTO = 0x5;

// The "cycle display brightness" frame reuses the OFF command byte and differs
// only in its fan nibble (0x88C00A6 versus 0x88C0051 for a real power-off).
// Mistaking one for the other is esphome/issues#2101.
static const uint8_t FAN_LIGHT_TOGGLE = 0xA;

// The temperature nibble holds (celsius - TEMPERATURE_OFFSET).
static const uint8_t TEMPERATURE_OFFSET = 15;

static const float TEMPERATURE_MIN_C = 16.0f;
static const float TEMPERATURE_MAX_C = 30.0f;
static const float TEMPERATURE_STEP_C = 1.0f;

// ===========================================================================

class LgPortableAcClimate : public climate_ir::ClimateIR {
 public:
  LgPortableAcClimate()
      : climate_ir::ClimateIR(TEMPERATURE_MIN_C, TEMPERATURE_MAX_C, TEMPERATURE_STEP_C,
                              /* supports_dry */ true, /* supports_fan_only */ true,
                              {climate::CLIMATE_FAN_LOW, climate::CLIMATE_FAN_HIGH}) {}

  void set_header_high(uint32_t value) { this->header_high_ = value; }
  void set_header_low(uint32_t value) { this->header_low_ = value; }
  void set_bit_high(uint32_t value) { this->bit_high_ = value; }
  void set_bit_one_low(uint32_t value) { this->bit_one_low_ = value; }
  void set_bit_zero_low(uint32_t value) { this->bit_zero_low_ = value; }
  void set_frame_bits(uint8_t value) { this->frame_bits_ = value; }
  void set_chunk_bits(uint8_t value) { this->chunk_bits_ = value; }
  void set_chunk_gap_low(uint32_t value) { this->chunk_gap_low_ = value; }
  void set_checksum_type(ChecksumType value) { this->checksum_type_ = value; }
  void set_supports_swing(bool value) { this->supports_swing_ = value; }
  void set_carrier_frequency(uint32_t value) { this->carrier_frequency_ = value; }

  void dump_config() override;

  /** Transmit an arbitrary frame.
   *
   * The remote has buttons that do not map onto a climate entity at all: Light
   * cycles the display brightness, Timer counts hours, and on some models Swing
   * steps through louvre positions. Rather than inventing entities for each, this
   * lets any frame you discover during capture be bound to a button or select in
   * YAML, with no recompile. Set `recalculate_checksum` false to send a captured
   * frame byte for byte.
   */
  void send_frame(uint64_t frame, bool recalculate_checksum);

 protected:
  climate::ClimateTraits traits() override;
  void control(const climate::ClimateCall &call) override;
  void transmit_state() override;
  bool on_receive(remote_base::RemoteReceiveData data) override;

  /// Build the frame for the current entity state, checksum included.
  uint64_t encode_state_();
  /// Apply a received frame to the entity state. False if it is not for us.
  bool decode_frame_(uint64_t frame);
  /// Put the pulse train for `frame` on the wire.
  void transmit_frame_(uint64_t frame);

  uint64_t compute_checksum_(uint64_t frame) const;
  void apply_checksum_(uint64_t &frame) const;
  bool checksum_valid_(uint64_t frame) const;

  uint8_t command_for_mode_(climate::ClimateMode mode, bool was_off) const;
  uint8_t fan_code_for_mode_(climate::ClimateFanMode fan_mode) const;
  BitField signature_field_() const {
    return BitField{(uint8_t) (this->frame_bits_ - SIGNATURE_WIDTH), SIGNATURE_WIDTH};
  }
  /// True for modes where the remote transmits the setpoint.
  bool mode_carries_setpoint_(climate::ClimateMode mode) const;

  uint32_t header_high_{3200};
  uint32_t header_low_{9900};
  uint32_t bit_high_{500};
  uint32_t bit_one_low_{1600};
  uint32_t bit_zero_low_{550};
  uint32_t chunk_gap_low_{8000};
  uint32_t carrier_frequency_{38000};
  uint8_t frame_bits_{28};
  uint8_t chunk_bits_{0};
  ChecksumType checksum_type_{CHECKSUM_NIBBLE_SUM};
  bool supports_swing_{false};

  /// The mode the unit was in before the pending change, so we can pick between
  /// the "switch on" and "change mode" commands.
  climate::ClimateMode mode_before_{climate::CLIMATE_MODE_OFF};
  /// Swing is a toggle on the remote, so it needs its own one-shot command
  /// rather than being folded into the state frame.
  bool send_swing_command_{false};
};

template<typename... Ts>
class SendFrameAction : public Action<Ts...>, public Parented<LgPortableAcClimate> {
 public:
  TEMPLATABLE_VALUE(uint64_t, frame)
  TEMPLATABLE_VALUE(bool, recalculate_checksum)

  void play(const Ts &...x) override {
    this->parent_->send_frame(this->frame_.value(x...), this->recalculate_checksum_.value(x...));
  }
};

}  // namespace lg_portable_ac
}  // namespace esphome
