"""ATEQ unit-code decoding shared by runtime capture and the Web UI."""

ATEQ_UNIT_ABBREVIATIONS = {
    0: "cm³/s",
    1000: "cm³/min",
    2000: "cm³/h",
    3000: "mm³/h",
    4000: "Pa(cal)",
    5000: "Pa/s(cal)",
    6000: "Pa",
    7000: "Pa(HR)",
    8000: "Pa/s",
    9000: "Pa/s(HR)",
    11000: "bar",
    12000: "kPa",
    13000: "psi",
    14000: "mbar",
    15000: "MPa",
    30000: "L/h",
    43000: "Pa(D)",
    44000: "Pa(LR)",
    45000: "Pa/s(LR)",
    46000: "in³/s",
    47000: "in³/min",
    48000: "in³/h",
    49000: "ft³/h",
    50000: "mL/s",
    51000: "mL/min",
    52000: "mL/h",
    58000: "cm³/s",
    59000: "cm³/min",
    60000: "cm³/h",
    76000: "ft³/s",
    77000: "ft³/min",
}


def decode_ateq_uint32(low_register, high_register):
    """Decode ATEQ's byte-swapped 32-bit value from two Modbus registers."""
    return (
        ((high_register & 0xFF) << 24)
        | ((high_register >> 8) << 16)
        | ((low_register & 0xFF) << 8)
        | (low_register >> 8)
    )


def get_ateq_unit_abbreviation(unit_code):
    """Return the display abbreviation while preserving unknown unit codes."""
    return ATEQ_UNIT_ABBREVIATIONS.get(unit_code, f"Unit({unit_code})")
