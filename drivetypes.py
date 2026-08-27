"""
drivetypes.py - what kind of drive is this, and how full should it get?

Each type keeps a different amount free, for a different reason:

    SSD    15%  they slow down sharply when nearly full
    HDD    10%  no wear, but a large file needs contiguous room to land
    USB    15%  cheap flash, no TRIM, so writes get worse as it fills
    SDC    15%  same as USB. A card is a card whatever the slot
    Phone  10%  the phone itself suffers first when it runs out

Cold storage relaxes that to 3%, but only where it is safe. Cards and sticks
have no stated retention unpowered and fail all at once rather than
gradually, so "fill it and leave it in a drawer" is exactly what ruins them
and the option is refused. A phone is powered daily and refreshes itself, so
it counts as ordinary storage throughout.
"""

import re

SSD = "ssd"
HDD = "hdd"
USB = "usb"
SDC = "sdc"
PHONE = "phone"
UNKNOWN = "unknown"

# What is left on a cold storage drive is for the filesystem, not performance.
COLD_STORAGE_FREE_PCT = 3

# Removable flash. A card and a stick share every rule here.
FLASH_TYPES = (USB, SDC)

# Every type that can be stored against a drive. UNKNOWN is only ever a
# fallback, never a choice.
ALL_TYPES = (SSD, HDD, USB, SDC, PHONE)

# warn_free_pct is the threshold; reason is shown when a drive is under it.
SPACE_RULES = {
    SSD: {
        "warn_free_pct": 15,
        "label": "SSD",
        "reason": "SSDs need spare room to write efficiently and wear evenly.",
    },
    HDD: {
        "warn_free_pct": 10,
        "label": "HDD",
        "reason": "No harm in being full, but new files have less contiguous room "
                  "and land on the slower inner tracks. Move something off, or "
                  "mark it as cold storage if you only read from it.",
    },
    USB: {
        "warn_free_pct": 15,
        "label": "USB",
        "reason": "Flash behind a cheap controller with no TRIM, so the fuller it "
                  "gets the harder each write is on it. Keep it as an extra copy "
                  "rather than the only one.",
    },
    SDC: {
        "warn_free_pct": 15,
        "label": "SD Card",
        "reason": "Flash behind a cheap controller with no TRIM, so the fuller it "
                  "gets the harder each write is on it. Cards also fail all at "
                  "once rather than gradually, so never keep the only copy here.",
    },
    PHONE: {
        "warn_free_pct": 10,
        "label": "Phone",
        "reason": "A phone needs working room, and it is the device itself that "
                  "suffers first when it runs out.",
    },
    UNKNOWN: {
        "warn_free_pct": 15,
        "label": "Unknown",
        "reason": "Type not known, so the cautious threshold is used. "
                  "Set the type to get the right one.",
    },
}

# Reads the type off a volume label like "SSD-500GB-01" or "PHO-256GB-01".
# The separator is optional so "SSD1" works. Longer names come first so "SSD"
# is never read as an "SD" card.
LABEL_PATTERN = re.compile(
    r"^\s*(ssd|nvme|hdd|microsd|sdcard|sdc|sd|usb|flash|thumb|phone|pho)\b[-_ ]?",
    re.IGNORECASE,
)

LABEL_ALIASES = {
    "ssd": SSD,
    "nvme": SSD,
    "hdd": HDD,
    "usb": USB,
    "flash": USB,
    "thumb": USB,
    "sdc": SDC,
    "sd": SDC,
    "microsd": SDC,
    "sdcard": SDC,
    "phone": PHONE,
    "pho": PHONE,
}

# Bus types from scanner.get_bus_type() that name the medium on their own. A
# card in a USB reader reports USB, not SD, which costs nothing since the two
# share every rule.
BUS_TYPES = {
    "SD": SDC,
    "MMC": SDC,
    "NVMe": SSD,
}


def from_label(label):
    """Work out the type from a volume label, or None if it does not say."""
    if not label:
        return None
    match = LABEL_PATTERN.match(label)
    if not match:
        return None
    return LABEL_ALIASES.get(match.group(1).lower())


def detect(label=None, removable=False, stored=None, bus_type=None):
    """
    A drive's type, in order of trust: set by hand, volume label, bus, then
    removable means a stick. Unknown uses the cautious threshold.
    """
    if stored in ALL_TYPES:
        return stored
    from_name = from_label(label)
    if from_name:
        return from_name
    from_bus = BUS_TYPES.get(bus_type)
    if from_bus:
        return from_bus
    if removable:
        return USB
    return UNKNOWN


def rule_for(drive_type):
    return SPACE_RULES.get(drive_type, SPACE_RULES[UNKNOWN])


def is_flash(drive_type):
    """A card or a stick. Not a phone, which is a better tier."""
    return drive_type in FLASH_TYPES


def allows_cold_storage(drive_type):
    """Refused for cards and sticks: unpowered flash has no stated retention,
    so filling one and leaving it in a drawer is what loses the data."""
    return not is_flash(drive_type)


def evaluate(drive_type, free_bytes, total_bytes, cold_storage=False):
    """
    Should this drive be flagged? Returns free_pct, threshold_pct, low,
    severity ('ok', 'warn', 'critical', 'cold') and a message.
    """
    total = total_bytes or 0
    free_pct = (100.0 * (free_bytes or 0) / total) if total else 0.0
    rule = rule_for(drive_type)
    threshold = rule["warn_free_pct"]

    # A cold storage flag left on a card is ignored, not obeyed. It could only
    # have been set before the type was known, and honouring it would mute the
    # warning on the drive that most needs it.
    if cold_storage and allows_cold_storage(drive_type):
        threshold = COLD_STORAGE_FREE_PCT
        low = free_pct < threshold
        return {
            "free_pct": round(free_pct, 1),
            "threshold_pct": threshold,
            "low": low,
            "severity": "critical" if low else "cold",
            "message": ("Cold storage and genuinely nearly full. Nothing more "
                        "will fit." if low else
                        "Cold storage, kept full on purpose and only read from."),
        }

    low = free_pct < threshold
    # Well past the threshold is worth calling out more loudly.
    severity = "ok"
    if low:
        severity = "critical" if free_pct < threshold / 2 else "warn"

    return {
        "free_pct": round(free_pct, 1),
        "threshold_pct": threshold,
        "low": low,
        "severity": severity,
        "message": rule["reason"] if low else "",
    }
