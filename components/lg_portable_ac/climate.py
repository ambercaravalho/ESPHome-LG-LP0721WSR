from esphome import automation
import esphome.codegen as cg
from esphome.components import climate_ir
import esphome.config_validation as cv
from esphome.const import CONF_CARRIER_FREQUENCY, CONF_ID, CONF_SUPPORTS_HEAT

AUTO_LOAD = ["climate_ir"]
CODEOWNERS = ["@ambercaravalho"]

lg_portable_ac_ns = cg.esphome_ns.namespace("lg_portable_ac")
LgPortableAcClimate = lg_portable_ac_ns.class_("LgPortableAcClimate", climate_ir.ClimateIR)
SendRawFrameAction = lg_portable_ac_ns.class_(
    "SendRawFrameAction", automation.Action, cg.Parented.template(LgPortableAcClimate)
)
SetTimerAction = lg_portable_ac_ns.class_(
    "SetTimerAction", automation.Action, cg.Parented.template(LgPortableAcClimate)
)
LightToggleAction = lg_portable_ac_ns.class_(
    "LightToggleAction", automation.Action, cg.Parented.template(LgPortableAcClimate)
)

CONF_HEADER_HIGH = "header_high"
CONF_HEADER_LOW = "header_low"
CONF_BIT_HIGH = "bit_high"
CONF_BIT_ONE_LOW = "bit_one_low"
CONF_BIT_ZERO_LOW = "bit_zero_low"
CONF_SUPPORTS_SWING = "supports_swing"
CONF_FRAME = "frame"
CONF_RECALCULATE_CHECKSUM = "recalculate_checksum"
CONF_HOURS = "hours"

# Frames are a fixed 14 bytes; anything else is a mistake worth catching in
# validation rather than at runtime.
FRAME_BYTES = 14
TIMER_MAX_HOURS = 24


def validate_config(config):
    """A one is the *longer* space, so swapping the two silently inverts every bit."""
    if config[CONF_BIT_ONE_LOW] <= config[CONF_BIT_ZERO_LOW]:
        raise cv.Invalid(
            f"{CONF_BIT_ONE_LOW} must be longer than {CONF_BIT_ZERO_LOW}; "
            "a one is encoded as the longer space",
            path=[CONF_BIT_ONE_LOW],
        )
    # Inherited from climate_ir, where it defaults to true. Rejecting it beats
    # accepting it and quietly doing nothing, which is what would happen: the
    # mode byte for heat has never been captured, so there is no frame to send.
    if config[CONF_SUPPORTS_HEAT]:
        raise cv.Invalid(
            "heat is not supported: the LP0721WSR is cooling only, and the mode "
            "byte used by heat-pump variants such as the LP0721SHR has not been "
            "captured, so enabling this would transmit a guess. Capture a heat "
            "frame and add it to the component first; see PROTOCOL.md",
            path=[CONF_SUPPORTS_HEAT],
        )
    return config


CONFIG_SCHEMA = cv.All(
    climate_ir.climate_ir_with_receiver_schema(LgPortableAcClimate).extend(
        {
            # Set false to hide the swing control on units with a fixed vane.
            # Harmless to leave on: the bits are simply ignored by such a unit.
            cv.Optional(CONF_SUPPORTS_SWING, default=True): cv.boolean,
            cv.Optional(CONF_SUPPORTS_HEAT, default=False): cv.boolean,
            # Measured from the LP0721WSR remote across 86 captures. These are
            # not the LG defaults and not inherited from another model: the
            # header space in particular is ~1590us, where other LG remotes use
            # ~4000us or ~9900us. See PROTOCOL.md.
            cv.Optional(
                CONF_HEADER_HIGH, default="3150us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_HEADER_LOW, default="1590us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(CONF_BIT_HIGH, default="550us"): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_BIT_ONE_LOW, default="1070us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(
                CONF_BIT_ZERO_LOW, default="290us"
            ): cv.positive_time_period_microseconds,
            cv.Optional(CONF_CARRIER_FREQUENCY, default="38000Hz"): cv.All(
                cv.frequency, cv.int_
            ),
        }
    ),
    validate_config,
)


SEND_RAW_FRAME_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(LgPortableAcClimate),
        cv.Required(CONF_FRAME): cv.All(
            [cv.hex_uint8_t], cv.Length(min=FRAME_BYTES, max=FRAME_BYTES)
        ),
        cv.Optional(CONF_RECALCULATE_CHECKSUM, default=True): cv.templatable(cv.boolean),
    }
)


@automation.register_action(
    "lg_portable_ac.send_raw_frame",
    SendRawFrameAction,
    SEND_RAW_FRAME_SCHEMA,
    synchronous=True,
)
async def send_raw_frame_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_frame(config[CONF_FRAME]))
    recalculate = await cg.templatable(config[CONF_RECALCULATE_CHECKSUM], args, bool)
    cg.add(var.set_recalculate_checksum(recalculate))
    return var


SET_TIMER_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(LgPortableAcClimate),
        cv.Required(CONF_HOURS): cv.templatable(cv.int_range(min=0, max=TIMER_MAX_HOURS)),
    }
)


@automation.register_action(
    "lg_portable_ac.set_timer", SetTimerAction, SET_TIMER_SCHEMA, synchronous=True
)
async def set_timer_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    hours = await cg.templatable(config[CONF_HOURS], args, cg.uint8)
    cg.add(var.set_hours(hours))
    return var


LIGHT_TOGGLE_SCHEMA = automation.maybe_simple_id(
    {cv.GenerateID(): cv.use_id(LgPortableAcClimate)}
)


@automation.register_action(
    "lg_portable_ac.light_toggle",
    LightToggleAction,
    LIGHT_TOGGLE_SCHEMA,
    synchronous=True,
)
async def light_toggle_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var


async def to_code(config):
    var = await climate_ir.new_climate_ir(config)

    cg.add(var.set_header_high(config[CONF_HEADER_HIGH]))
    cg.add(var.set_header_low(config[CONF_HEADER_LOW]))
    cg.add(var.set_bit_high(config[CONF_BIT_HIGH]))
    cg.add(var.set_bit_one_low(config[CONF_BIT_ONE_LOW]))
    cg.add(var.set_bit_zero_low(config[CONF_BIT_ZERO_LOW]))
    cg.add(var.set_supports_swing(config[CONF_SUPPORTS_SWING]))
    cg.add(var.set_carrier_frequency(config[CONF_CARRIER_FREQUENCY]))
