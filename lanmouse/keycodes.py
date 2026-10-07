"""Physical Linux evdev keys to PC set-1 scan codes (layout handled by target OS)."""

# Most evdev codes 1..83 match set-1, excluding the function keys handled below.
SCAN = {code: (code, False) for code in range(1, 84)}
SCAN.update(
    {
        86: (0x56, False),
        87: (0x57, False),
        88: (0x58, False),
        96: (0x1C, True),
        97: (0x1D, True),
        98: (0x35, True),
        99: (0x37, True),
        100: (0x38, True),
        102: (0x47, True),
        103: (0x48, True),
        104: (0x49, True),
        105: (0x4B, True),
        106: (0x4D, True),
        107: (0x4F, True),
        108: (0x50, True),
        109: (0x51, True),
        110: (0x52, True),
        111: (0x53, True),
        119: (0x45, True),
        125: (0x5B, True),
        126: (0x5C, True),
        127: (0x5D, True),
        113: (0x20, True),
        114: (0x2E, True),
        115: (0x30, True),
        163: (0x19, True),
        164: (0x22, True),
        165: (0x10, True),
        166: (0x24, True),
    }
)
REVERSE_SCAN = {value: code for code, value in SCAN.items()}
F8 = 66
