from esphome import automation
import esphome.codegen as cg
from esphome.components import climate_ir
import esphome.config_validation as cv
from esphome.const import CONF_CARRIER_FREQUENCY, CONF_ID, CONF_SUPPORTS_HEAT

AUTO_LOAD = ["climate_ir"]
CODEOWNERS = ["@ambercaravalho"]

lg_portable_ac_ns = cg.esphome_ns.namespace("lg_portable_ac")
LgPortableAcClimate = lg_portable_ac_ns.class_("LgPortableAcClimate", climate_ir.ClimateIR)
ChecksumType = lg_portable_ac_ns.enum("ChecksumType")
SendFrameAction = lg_portable_ac_ns.class_(
    "SendFrameAction", automation.Action, cg.Parented.template(LgPortableAcClimate)
)

CONF_HEADER_HIGH = "header_high"
CONF_HEADER_LOW = "header_low"
CONF_BIT_HIGH = "bit_high"
CONF_BIT_ONE_LOW = "bit_one_low"
CONF_BIT_ZERO_LOW = "bit_zero_low"
CONF_FRAME_BITS = "frame_bits"
CONF_CHUNK_BITS = "chunk_bits"
CONF_CHUNK_GAP_LOW = "chunk_gap_low"
CONF_CHECKSUM = "checksum"
CONF_SUPPORTS_SWING = "supports_swing"
CONF_FRAME = "frame"
CONF_RECALCULATE_CHECKSUM = "recalculate_checksum"

CHECKSUM_TYPES = {
    "none": ChecksumType.CHECKSUM_NONE,
    "nibble_sum": ChecksumType.CHECKSUM_NIBBLE_SUM,
    "byte_sum": ChecksumType.CHECKSUM_BYTE_SUM,
    "nibble_xor": ChecksumType.CHECKSUM_NIBBLE_XOR,
    "byte_xor": ChecksumType.CHECKSUM_BYTE_XOR,
}


def validate_frame(config):
    """The chunk size has to actually divide the frame into more than one piece."""
    chunk_bits = config[CONF_CHUNK_BITS]
    if chunk_bits and chunk_bits >= config[CONF_FRAME_BITS]:
        raise cv.Invalid(
            f"{CONF_CHUNK_BITS} ({chunk_bits}) must be smaller than "
            f"{CONF_FRAME_BITS} ({config[CONF_FRAME_BITS]}), or 0 to disable chunking",
            path=[CONF_CHUNK_BITS],
        )
    if config[CONF_BIT_ONE_LOW] <= config[CONF_BIT_ZERO_LOW]:
        raise cv.Invalid(
            f"{CONF_BIT_ONE_LOW} must be longer than {CONF_BIT_ZERO_LOW}; "
            "a one is encoded as the longer space",
            path=[CONF_BIT_ONE_LOW],
        )
    return config


CONFIG_SCHEMA = cv.All(
    climate_ir.climate_ir_with_receiver_schema(LgPortableAcClimate).extend(
        {
            # The LP0721WSR is cooling only. Heat-pump variants such as the
            # LP0721SHR need supports_heat: true.
            cv.Optional(CONF_SUPPORTS_HEAT, default=False): cv.boolean,
            cv.Optional(CONF_SUPPORTS_SWING, default=False): cv.boolean,
            # Timings. These default to the LG "long header" family (~3.2ms mark
            # followed by a ~9.9ms space), which is what portable and many
            # recent split remotes use. ESPHome's built-in climate_ir_lg
            # defaults to the older 8ms/4ms header instead, and getting this
            # wrong is the single most common reason an LG unit ignores you.
            cv.Optional(
                CONF_HEADER_HIGH, default="3200us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_HEADER_LOW, default="9900us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(CONF_BIT_HIGH, default="500us"): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_BIT_ONE_LOW, default="1600us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_BIT_ZERO_LOW, default="550us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(CONF_FRAME_BITS, default=28): cv.int_range(min=8, max=64),
            # Some LG AC frames are split into chunks separated by a long gap.
            # 0 disables it, which is right for the 28-bit frame.
            cv.Optional(CONF_CHUNK_BITS, default=0): cv.int_range(min=0, max=64),
            cv.Optional(
                CONF_CHUNK_GAP_LOW, default="8000us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(CONF_CHECKSUM, default="nibble_sum"): cv.enum(
                CHECKSUM_TYPES, lower=True
            ),
            cv.Optional(CONF_CARRIER_FREQUENCY, default="38000Hz"): cv.All(
                cv.frequency, cv.int_
            ),
        }
    ),
    validate_frame,
)


SEND_FRAME_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(LgPortableAcClimate),
        cv.Required(CONF_FRAME): cv.templatable(cv.hex_uint64_t),
        cv.Optional(CONF_RECALCULATE_CHECKSUM, default=True): cv.templatable(cv.boolean),
    }
)


@automation.register_action(
    "lg_portable_ac.send_frame", SendFrameAction, SEND_FRAME_ACTION_SCHEMA
)
async def send_frame_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    frame = await cg.templatable(config[CONF_FRAME], args, cg.uint64)
    cg.add(var.set_frame(frame))
    recalculate = await cg.templatable(config[CONF_RECALCULATE_CHECKSUM], args, bool)
    cg.add(var.set_recalculate_checksum(recalculate))
    return var


async def to_code(config):
    var = await climate_ir.new_climate_ir(config)

    cg.add(var.set_header_high(config[CONF_HEADER_HIGH]))
    cg.add(var.set_header_low(config[CONF_HEADER_LOW]))
    cg.add(var.set_bit_high(config[CONF_BIT_HIGH]))
    cg.add(var.set_bit_one_low(config[CONF_BIT_ONE_LOW]))
    cg.add(var.set_bit_zero_low(config[CONF_BIT_ZERO_LOW]))
    cg.add(var.set_frame_bits(config[CONF_FRAME_BITS]))
    cg.add(var.set_chunk_bits(config[CONF_CHUNK_BITS]))
    cg.add(var.set_chunk_gap_low(config[CONF_CHUNK_GAP_LOW]))
    cg.add(var.set_checksum_type(config[CONF_CHECKSUM]))
    cg.add(var.set_supports_swing(config[CONF_SUPPORTS_SWING]))
    cg.add(var.set_carrier_frequency(config[CONF_CARRIER_FREQUENCY]))
