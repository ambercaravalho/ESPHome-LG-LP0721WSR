#include "lg_portable_ac.h"

#include <cmath>
#include <cstring>

#include "esphome/core/log.h"

namespace esphome {
namespace lg_portable_ac {

static const char *const TAG = "lg_portable_ac";

// ---------------------------------------------------------------------------
// Traits
// ---------------------------------------------------------------------------

climate::ClimateTraits LgPortableAcClimate::traits() {
  auto traits = climate_ir::ClimateIR::traits();

  // ClimateIR advertises HEAT_COOL unconditionally, but the Mode button cycles
  // only Cool, Dry and Fan: this unit has no auto mode. Replace the whole set,
  // since there is no public API for removing a single entry.
  traits.set_supported_modes({climate::CLIMATE_MODE_OFF, climate::CLIMATE_MODE_COOL,
                              climate::CLIMATE_MODE_DRY, climate::CLIMATE_MODE_FAN_ONLY});

  if (!this->supports_swing_) {
    traits.set_supported_swing_modes({});
  }

  return traits;
}

void LgPortableAcClimate::dump_config() {
  climate_ir::ClimateIR::dump_config();
  ESP_LOGCONFIG(TAG,
                "  Frame: %u bits, LSB-first, byte-sum checksum\n"
                "  Header: %" PRIu32 "us / %" PRIu32 "us\n"
                "  Bit mark: %" PRIu32 "us, one: %" PRIu32 "us, zero: %" PRIu32 "us\n"
                "  Carrier: %" PRIu32 "Hz\n"
                "  Supports swing: %s",
                FRAME_BITS, this->header_high_, this->header_low_, this->bit_high_, this->bit_one_low_,
                this->bit_zero_low_, this->carrier_frequency_, YESNO(this->supports_swing_));
}

// ---------------------------------------------------------------------------
// Checksum
// ---------------------------------------------------------------------------

uint8_t LgPortableAcClimate::checksum_(const Frame &frame) {
  uint8_t sum = 0;
  for (uint8_t i = 0; i < IDX_CHECKSUM; i++)
    sum += frame[i];
  return sum;
}

bool LgPortableAcClimate::prefix_valid_(const Frame &frame) {
  return std::memcmp(frame.data(), PREFIX, PREFIX_BYTES) == 0;
}

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

uint8_t LgPortableAcClimate::mode_byte_() const {
  // Powering off leaves the mode byte alone, so an off frame reports the mode
  // the unit was last running in rather than a distinct "off" mode.
  const climate::ClimateMode mode =
      this->mode == climate::CLIMATE_MODE_OFF ? this->last_active_mode_ : this->mode;
  switch (mode) {
    case climate::CLIMATE_MODE_DRY:
      return MODE_DRY;
    case climate::CLIMATE_MODE_FAN_ONLY:
      return MODE_FAN_ONLY;
    case climate::CLIMATE_MODE_COOL:
    default:
      return MODE_COOL;
  }
}

uint8_t LgPortableAcClimate::fan_byte_() const {
  uint8_t value = this->fan_mode == climate::CLIMATE_FAN_LOW ? FAN_LOW : FAN_HIGH;
  if (this->supports_swing_ && this->swing_mode == climate::CLIMATE_SWING_VERTICAL)
    value |= SWING_ON_VALUE << SWING_SHIFT;
  return value;
}

Frame LgPortableAcClimate::encode_state_() {
  if (this->mode != climate::CLIMATE_MODE_OFF)
    this->last_active_mode_ = this->mode;

  Frame frame{};
  std::memcpy(frame.data(), PREFIX, PREFIX_BYTES);

  frame[IDX_FLAGS] = FLAGS_BASE;
  if (this->mode != climate::CLIMATE_MODE_OFF)
    frame[IDX_FLAGS] |= FLAG_POWER;
  if (this->timer_hours_ != TIMER_OFF)
    frame[IDX_FLAGS] |= FLAG_TIMER;

  frame[IDX_MODE] = this->mode_byte_();

  // Byte 7 carries the setpoint in every mode, including Fan, where the unit
  // has no setpoint to act on. The remote transmits the last one regardless.
  const float target = clamp(this->target_temperature, TEMPERATURE_MIN_C, TEMPERATURE_MAX_C);
  const int celsius = (int) lroundf(target);
  frame[IDX_TEMPERATURE] = TEMPERATURE_BASE - (uint8_t) celsius;

  // A request that is not a whole degree Celsius came from a Fahrenheit UI, where
  // Home Assistant converts before sending: 78F arrives as 25.56C. Rounding that
  // to 26C and saying nothing else would ask for 78.8F, so state the Fahrenheit
  // value too, exactly as the remote does when its display is switched over. Byte 7
  // stays the rounded Celsius equivalent, which is what the remote sends alongside.
  if (fabsf(target - celsius) > WHOLE_CELSIUS_EPSILON)
    this->fahrenheit_ = true;
  if (this->fahrenheit_)
    frame[IDX_FAHRENHEIT] = FAHRENHEIT_FLAG | (uint8_t) lroundf(target * 9.0f / 5.0f + 32.0f);

  frame[IDX_FAN] = this->fan_byte_();
  frame[IDX_TIMER] = this->timer_hours_ * TIMER_UNITS_PER_HOUR;
  frame[IDX_CHECKSUM] = checksum_(frame);
  return frame;
}

// ---------------------------------------------------------------------------
// Transmit
// ---------------------------------------------------------------------------

void LgPortableAcClimate::transmit_state() {
  const Frame frame = this->encode_state_();
  ESP_LOGD(TAG, "sending %s", format_hex_pretty(frame.data(), frame.size()).c_str());
  this->transmit_frame_(frame);
}

void LgPortableAcClimate::send_light_toggle() {
  // A Light press is an ordinary state frame with one extra flag, so it cannot
  // disturb the mode, setpoint or fan. Nothing to publish: the brightness is not
  // reported by the protocol and the climate state has not changed.
  Frame frame = this->encode_state_();
  frame[IDX_FLAGS] |= FLAG_LIGHT;
  frame[IDX_CHECKSUM] = checksum_(frame);
  ESP_LOGD(TAG, "cycling display brightness: %s", format_hex_pretty(frame.data(), frame.size()).c_str());
  this->transmit_frame_(frame);
}

void LgPortableAcClimate::set_timer_hours(uint8_t hours) {
  if (hours > TIMER_MAX_HOURS) {
    ESP_LOGW(TAG, "timer of %uh exceeds the %uh maximum; clamping", hours, TIMER_MAX_HOURS);
    hours = TIMER_MAX_HOURS;
  }
  this->timer_hours_ = hours;
  // The timer rides in the state frame, so this is a normal state transmission
  // rather than a command of its own.
  this->transmit_state();
  this->publish_state();
}

void LgPortableAcClimate::send_raw_frame(const std::vector<uint8_t> &bytes, bool recalculate_checksum) {
  if (bytes.size() != FRAME_BYTES) {
    ESP_LOGE(TAG, "raw frame must be exactly %u bytes, got %u", FRAME_BYTES, (unsigned) bytes.size());
    return;
  }
  Frame frame{};
  std::copy(bytes.begin(), bytes.end(), frame.begin());
  if (recalculate_checksum)
    frame[IDX_CHECKSUM] = checksum_(frame);
  ESP_LOGD(TAG, "sending raw %s", format_hex_pretty(frame.data(), frame.size()).c_str());
  this->transmit_frame_(frame);
}

void LgPortableAcClimate::transmit_frame_(const Frame &frame) {
  auto transmit = this->transmitter_->transmit();
  auto *data = transmit.get_data();

  data->set_carrier_frequency(this->carrier_frequency_);
  // Two entries each for the header pair and every bit, plus the trailing mark.
  data->reserve(2 * (FRAME_BITS + 1) + 1);

  data->item(this->header_high_, this->header_low_);
  for (uint8_t index = 0; index < FRAME_BYTES; index++) {
    for (uint8_t bit = 0; bit < 8; bit++) {
      const bool one = (frame[index] >> bit) & 1;
      data->item(this->bit_high_, one ? this->bit_one_low_ : this->bit_zero_low_);
    }
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

  Frame frame{};
  for (uint8_t index = 0; index < FRAME_BYTES; index++) {
    for (uint8_t bit = 0; bit < 8; bit++) {
      if (data.expect_item(this->bit_high_, this->bit_one_low_)) {
        frame[index] |= 1 << bit;
      } else if (!data.expect_item(this->bit_high_, this->bit_zero_low_)) {
        return false;
      }
    }
  }

  return this->decode_frame_(frame);
}

bool LgPortableAcClimate::decode_frame_(const Frame &frame) {
  // The prefix is checked first and quietly: anything else on 38kHz in the room
  // lands here too, and a TV remote is not worth a warning.
  if (!prefix_valid_(frame)) {
    ESP_LOGV(TAG, "not an LG portable frame, ignoring");
    return false;
  }

  const uint8_t expected = checksum_(frame);
  if (frame[IDX_CHECKSUM] != expected) {
    ESP_LOGW(TAG, "checksum 0x%02X, expected 0x%02X, on %s", frame[IDX_CHECKSUM], expected,
             format_hex_pretty(frame.data(), frame.size()).c_str());
    return false;
  }

  climate::ClimateMode mode;
  switch (frame[IDX_MODE]) {
    case MODE_COOL:
      mode = climate::CLIMATE_MODE_COOL;
      break;
    case MODE_DRY:
      mode = climate::CLIMATE_MODE_DRY;
      break;
    case MODE_FAN_ONLY:
      mode = climate::CLIMATE_MODE_FAN_ONLY;
      break;
    default:
      // A mode this unit does not have, most likely heat from an SHR remote.
      // Refusing it is safer than picking the nearest match.
      ESP_LOGW(TAG, "unknown mode byte 0x%02X, ignoring frame", frame[IDX_MODE]);
      return false;
  }
  this->last_active_mode_ = mode;
  this->mode = (frame[IDX_FLAGS] & FLAG_POWER) ? mode : climate::CLIMATE_MODE_OFF;

  const uint8_t encoded_temp = frame[IDX_TEMPERATURE];
  const int target = TEMPERATURE_BASE - encoded_temp;
  if (target >= (int) TEMPERATURE_MIN_C && target <= (int) TEMPERATURE_MAX_C) {
    this->target_temperature = target;
  } else {
    // Out of range means the byte is not the setpoint we think it is; keeping the
    // old value beats showing a nonsensical one.
    ESP_LOGW(TAG, "setpoint byte 0x%02X decodes to %dC, out of range", encoded_temp, target);
  }

  // Byte 12 states the same setpoint more precisely, so prefer it where present.
  // Taking the rounded Celsius from byte 7 instead would make a Fahrenheit setpoint
  // drift by up to a degree F every time it round-tripped through us. Following the
  // sender's choice of unit also means the physical remote's display mode wins,
  // which is what someone pressing its unit button is asking for.
  this->fahrenheit_ = (frame[IDX_FAHRENHEIT] & FAHRENHEIT_FLAG) != 0;
  if (this->fahrenheit_) {
    const uint8_t fahrenheit = frame[IDX_FAHRENHEIT] & FAHRENHEIT_MASK;
    const float precise = (fahrenheit - 32) * 5.0f / 9.0f;
    if (precise >= TEMPERATURE_MIN_C - 1.0f && precise <= TEMPERATURE_MAX_C + 1.0f) {
      this->target_temperature = clamp(precise, TEMPERATURE_MIN_C, TEMPERATURE_MAX_C);
    }
    ESP_LOGD(TAG, "setpoint stated as %uF", fahrenheit);
  }

  switch (frame[IDX_FAN] & FAN_MASK) {
    case FAN_LOW:
      this->fan_mode = climate::CLIMATE_FAN_LOW;
      break;
    case FAN_HIGH:
      this->fan_mode = climate::CLIMATE_FAN_HIGH;
      break;
    default:
      ESP_LOGD(TAG, "unknown fan value 0x%02X, leaving fan mode alone", frame[IDX_FAN] & FAN_MASK);
      break;
  }

  if (this->supports_swing_) {
    const uint8_t swing = (frame[IDX_FAN] & SWING_MASK) >> SWING_SHIFT;
    this->swing_mode =
        swing == SWING_OFF_VALUE ? climate::CLIMATE_SWING_OFF : climate::CLIMATE_SWING_VERTICAL;
  }

  this->timer_hours_ = (frame[IDX_FLAGS] & FLAG_TIMER) ? frame[IDX_TIMER] / TIMER_UNITS_PER_HOUR : TIMER_OFF;

  // Logged rather than acted on: the frame says the brightness advanced one step,
  // but not to what, so there is nothing to publish. The rest of the frame is a
  // normal state report and has already been applied above.
  if (frame[IDX_FLAGS] & FLAG_LIGHT)
    ESP_LOGD(TAG, "display brightness advanced one step");

  this->publish_state();
  return true;
}

}  // namespace lg_portable_ac
}  // namespace esphome
