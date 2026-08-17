#include "lg_portable_ac.h"

#include "esphome/core/log.h"

namespace esphome {
namespace lg_portable_ac {

static const char *const TAG = "lg_portable_ac";

// ---------------------------------------------------------------------------
// Traits
// ---------------------------------------------------------------------------

climate::ClimateTraits LgPortableAcClimate::traits() {
  auto traits = climate_ir::ClimateIR::traits();

  // ClimateIR advertises HEAT_COOL unconditionally, but the LP-series has no
  // auto/AI mode: the Mode button cycles Cool, Dry, Fan and (on SHR models)
  // Heat. Replace the whole set rather than trying to remove one entry, since
  // erase() is not part of the public traits API.
  if (this->supports_heat_) {
    traits.set_supported_modes({climate::CLIMATE_MODE_OFF, climate::CLIMATE_MODE_COOL,
                                climate::CLIMATE_MODE_DRY, climate::CLIMATE_MODE_FAN_ONLY,
                                climate::CLIMATE_MODE_HEAT});
  } else {
    traits.set_supported_modes({climate::CLIMATE_MODE_OFF, climate::CLIMATE_MODE_COOL,
                                climate::CLIMATE_MODE_DRY, climate::CLIMATE_MODE_FAN_ONLY});
  }

  if (this->supports_swing_) {
    traits.set_supported_swing_modes({climate::CLIMATE_SWING_OFF, climate::CLIMATE_SWING_VERTICAL});
  }

  return traits;
}

void LgPortableAcClimate::dump_config() {
  climate_ir::ClimateIR::dump_config();
  static const char *const CHECKSUM_NAMES[] = {"none", "nibble sum", "byte sum", "nibble xor",
                                               "byte xor"};
  ESP_LOGCONFIG(TAG,
                "  Frame bits: %u\n"
                "  Header: %" PRIu32 "us / %" PRIu32 "us\n"
                "  Bit mark: %" PRIu32 "us, one: %" PRIu32 "us, zero: %" PRIu32 "us\n"
                "  Carrier: %" PRIu32 "Hz\n"
                "  Checksum: %s\n"
                "  Supports swing: %s",
                this->frame_bits_, this->header_high_, this->header_low_, this->bit_high_,
                this->bit_one_low_, this->bit_zero_low_, this->carrier_frequency_,
                CHECKSUM_NAMES[this->checksum_type_], YESNO(this->supports_swing_));
  if (this->chunk_bits_ > 0) {
    ESP_LOGCONFIG(TAG, "  Chunked every %u bits with a %" PRIu32 "us gap", this->chunk_bits_,
                  this->chunk_gap_low_);
  }
}

// ---------------------------------------------------------------------------
// Control
// ---------------------------------------------------------------------------

void LgPortableAcClimate::control(const climate::ClimateCall &call) {
  // Swing is a toggle on the remote: there is no "swing off" frame, only a
  // "flip the swing" frame. Notice the change here, before ClimateIR overwrites
  // the current state, so transmit_state() knows to send that command instead
  // of a normal state frame.
  auto swing_mode = call.get_swing_mode();
  if (this->supports_swing_ && swing_mode.has_value() && *swing_mode != this->swing_mode) {
    this->send_swing_command_ = true;
  }
  climate_ir::ClimateIR::control(call);
}

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

bool LgPortableAcClimate::mode_carries_setpoint_(climate::ClimateMode mode) const {
  // Per the owner's manual the up/down buttons adjust the setpoint in Cool, Dry
  // and Heat. In Fan mode the unit has no setpoint at all.
  return mode == climate::CLIMATE_MODE_COOL || mode == climate::CLIMATE_MODE_HEAT ||
         mode == climate::CLIMATE_MODE_DRY;
}

uint8_t LgPortableAcClimate::command_for_mode_(climate::ClimateMode mode, bool was_off) const {
  switch (mode) {
    case climate::CLIMATE_MODE_COOL:
      return was_off ? CMD_ON_COOL : CMD_COOL;
    case climate::CLIMATE_MODE_DRY:
      return was_off ? CMD_ON_DRY : CMD_DRY;
    case climate::CLIMATE_MODE_FAN_ONLY:
      return was_off ? CMD_ON_FAN_ONLY : CMD_FAN_ONLY;
    case climate::CLIMATE_MODE_HEAT:
      return was_off ? CMD_ON_HEAT : CMD_HEAT;
    case climate::CLIMATE_MODE_OFF:
    default:
      return CMD_OFF;
  }
}

uint8_t LgPortableAcClimate::fan_code_for_mode_(climate::ClimateFanMode fan_mode) const {
  switch (fan_mode) {
    case climate::CLIMATE_FAN_LOW:
      return FAN_LOW;
    case climate::CLIMATE_FAN_MEDIUM:
      return FAN_MEDIUM;
    case climate::CLIMATE_FAN_HIGH:
      return FAN_HIGH;
    default:
      return FAN_AUTO;
  }
}

uint64_t LgPortableAcClimate::encode_state_() {
  uint64_t frame = 0;
  this->signature_field_().set(frame, SIGNATURE);

  if (this->send_swing_command_) {
    this->send_swing_command_ = false;
    FIELD_COMMAND.set(frame, CMD_SWING);
    FIELD_FAN.set(frame, FAN_AUTO);
    this->apply_checksum_(frame);
    return frame;
  }

  const bool was_off = this->mode_before_ == climate::CLIMATE_MODE_OFF;
  FIELD_COMMAND.set(frame, this->command_for_mode_(this->mode, was_off));
  this->mode_before_ = this->mode;

  if (this->mode == climate::CLIMATE_MODE_OFF) {
    // The remote parks the fan nibble at AUTO when powering down.
    FIELD_FAN.set(frame, FAN_AUTO);
  } else {
    FIELD_FAN.set(frame, this->fan_code_for_mode_(this->fan_mode.value_or(climate::CLIMATE_FAN_HIGH)));
    if (this->mode_carries_setpoint_(this->mode)) {
      const float target = clamp(this->target_temperature, TEMPERATURE_MIN_C, TEMPERATURE_MAX_C);
      FIELD_TEMPERATURE.set(frame, (uint8_t) roundf(target) - TEMPERATURE_OFFSET);
    }
  }

  this->apply_checksum_(frame);
  return frame;
}

// ---------------------------------------------------------------------------
// Checksum
// ---------------------------------------------------------------------------

uint64_t LgPortableAcClimate::compute_checksum_(uint64_t frame) const {
  // The payload is everything above the checksum field. Walk it in whichever
  // unit the algorithm needs, most significant first.
  const uint8_t payload_bits = this->frame_bits_ - FIELD_CHECKSUM.width;
  uint64_t accumulator = 0;

  switch (this->checksum_type_) {
    case CHECKSUM_NIBBLE_SUM:
    case CHECKSUM_NIBBLE_XOR:
      for (int8_t shift = payload_bits - 4; shift >= 0; shift -= 4) {
        const uint64_t nibble = (frame >> shift) & 0xF;
        accumulator = this->checksum_type_ == CHECKSUM_NIBBLE_SUM ? accumulator + nibble
                                                                 : accumulator ^ nibble;
      }
      break;
    case CHECKSUM_BYTE_SUM:
    case CHECKSUM_BYTE_XOR:
      for (int8_t shift = payload_bits - 8; shift >= 0; shift -= 8) {
        const uint64_t byte = (frame >> shift) & 0xFF;
        accumulator =
            this->checksum_type_ == CHECKSUM_BYTE_SUM ? accumulator + byte : accumulator ^ byte;
      }
      break;
    case CHECKSUM_NONE:
    default:
      return FIELD_CHECKSUM.get(frame);
  }

  return accumulator & FIELD_CHECKSUM.value_mask();
}

void LgPortableAcClimate::apply_checksum_(uint64_t &frame) const {
  if (this->checksum_type_ == CHECKSUM_NONE)
    return;
  FIELD_CHECKSUM.set(frame, this->compute_checksum_(frame));
}

bool LgPortableAcClimate::checksum_valid_(uint64_t frame) const {
  if (this->checksum_type_ == CHECKSUM_NONE)
    return true;
  return FIELD_CHECKSUM.get(frame) == this->compute_checksum_(frame);
}

// ---------------------------------------------------------------------------
// Transmit
// ---------------------------------------------------------------------------

void LgPortableAcClimate::transmit_state() {
  const uint64_t frame = this->encode_state_();
  ESP_LOGD(TAG, "sending 0x%" PRIX64 " (%u bits)", frame, this->frame_bits_);
  this->transmit_frame_(frame);
}

void LgPortableAcClimate::send_frame(uint64_t frame, bool recalculate_checksum) {
  if (recalculate_checksum)
    this->apply_checksum_(frame);
  ESP_LOGD(TAG, "sending frame 0x%" PRIX64 " on request", frame);
  this->transmit_frame_(frame);
}

void LgPortableAcClimate::transmit_frame_(uint64_t frame) {
  auto transmit = this->transmitter_->transmit();
  auto *data = transmit.get_data();

  data->set_carrier_frequency(this->carrier_frequency_);
  // Header, one item per bit, an extra item per chunk gap, and the trailing mark.
  data->reserve(4 + this->frame_bits_ * 2u);

  data->item(this->header_high_, this->header_low_);
  for (uint8_t index = 0; index < this->frame_bits_; index++) {
    if (this->chunk_bits_ > 0 && index > 0 && index % this->chunk_bits_ == 0) {
      data->item(this->bit_high_, this->chunk_gap_low_);
    }
    const bool bit = (frame >> (this->frame_bits_ - 1 - index)) & 1ULL;
    data->item(this->bit_high_, bit ? this->bit_one_low_ : this->bit_zero_low_);
  }
  data->mark(this->bit_high_);

  transmit.perform();
}

// ---------------------------------------------------------------------------
// Receive
// ---------------------------------------------------------------------------

bool LgPortableAcClimate::on_receive(remote_base::RemoteReceiveData data) {
  if (!data.expect_item(this->header_high_, this->header_low_))
    return false;

  uint64_t frame = 0;
  for (uint8_t index = 0; index < this->frame_bits_; index++) {
    if (this->chunk_bits_ > 0 && index > 0 && index % this->chunk_bits_ == 0) {
      if (!data.expect_item(this->bit_high_, this->chunk_gap_low_))
        return false;
    }
    if (data.expect_item(this->bit_high_, this->bit_one_low_)) {
      frame = (frame << 1) | 1ULL;
    } else if (data.expect_item(this->bit_high_, this->bit_zero_low_)) {
      frame <<= 1;
    } else {
      return false;
    }
  }

  ESP_LOGD(TAG, "received 0x%" PRIX64 " (%u bits)", frame, this->frame_bits_);
  return this->decode_frame_(frame);
}

bool LgPortableAcClimate::decode_frame_(uint64_t frame) {
  if (this->signature_field_().get(frame) != SIGNATURE) {
    ESP_LOGV(TAG, "signature mismatch, not our protocol");
    return false;
  }
  if (!this->checksum_valid_(frame)) {
    ESP_LOGW(TAG, "checksum mismatch on 0x%" PRIX64 ": expected 0x%" PRIX64 ", got 0x%" PRIX64,
             frame, this->compute_checksum_(frame), FIELD_CHECKSUM.get(frame));
    return false;
  }

  const uint8_t command = (uint8_t) FIELD_COMMAND.get(frame);

  // Check this before the power-off branch: the display-brightness frame shares
  // the OFF command byte, so treating it as a power-off would make the entity
  // report OFF every time somebody dims the display.
  if (command == CMD_OFF && FIELD_FAN.get(frame) == FAN_LIGHT_TOGGLE) {
    ESP_LOGD(TAG, "display brightness toggle; leaving climate state alone");
    return true;
  }

  if (command == CMD_OFF) {
    this->mode = climate::CLIMATE_MODE_OFF;
    this->mode_before_ = this->mode;
    this->publish_state();
    return true;
  }

  if (command == CMD_SWING) {
    // Toggle, so the frame says "flip it" and we track which side we are on.
    this->swing_mode = this->swing_mode == climate::CLIMATE_SWING_OFF
                           ? climate::CLIMATE_SWING_VERTICAL
                           : climate::CLIMATE_SWING_OFF;
    this->publish_state();
    return true;
  }

  switch (command) {
    case CMD_ON_DRY:
    case CMD_DRY:
      this->mode = climate::CLIMATE_MODE_DRY;
      break;
    case CMD_ON_FAN_ONLY:
    case CMD_FAN_ONLY:
      this->mode = climate::CLIMATE_MODE_FAN_ONLY;
      break;
    case CMD_ON_HEAT:
    case CMD_HEAT:
      this->mode = climate::CLIMATE_MODE_HEAT;
      break;
    case CMD_ON_COOL:
    case CMD_COOL:
      this->mode = climate::CLIMATE_MODE_COOL;
      break;
    default:
      ESP_LOGD(TAG, "unrecognised command 0x%02X, ignoring", command);
      return false;
  }
  this->mode_before_ = this->mode;

  switch (FIELD_FAN.get(frame)) {
    case FAN_LOW:
      this->fan_mode = climate::CLIMATE_FAN_LOW;
      break;
    case FAN_MEDIUM:
      this->fan_mode = climate::CLIMATE_FAN_MEDIUM;
      break;
    case FAN_HIGH:
      this->fan_mode = climate::CLIMATE_FAN_HIGH;
      break;
    default:
      this->fan_mode = climate::CLIMATE_FAN_AUTO;
      break;
  }

  if (this->mode_carries_setpoint_(this->mode)) {
    this->target_temperature = FIELD_TEMPERATURE.get(frame) + TEMPERATURE_OFFSET;
  }

  this->publish_state();
  return true;
}

}  // namespace lg_portable_ac
}  // namespace esphome
