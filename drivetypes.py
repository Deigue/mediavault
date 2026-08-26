"""
drivetypes.py - what kind of drive is this, and how full should it get?

The sensible fill level differs by type, and for mechanical disks it also
depends on what the drive is for.

SSDs slow down as they fill, mildly from about 85 percent and sharply past
95, because the controller uses whatever space is unallocated as room to
shuffle blocks around. Modern controllers do that with any free space on the
drive, so setting aside a fixed reserve is no longer the practice it once
was; simply not running near full is what matters. Only writes wear the
cells, reads cost nothing.

Mechanical disks do not wear from being full at all, and this library is
made of large files read start to finish. Fragmentation costs one seek per
extent, so a 4 GB film split across ten extents loses about a tenth of a
second on a read lasting forty. The familiar "keep 15 to 20 percent free"
advice is aimed at general purpose drives full of small files, not at this.

What does still matter on a full mechanical disk is writing: the space left
is on the slower inner tracks and scattered, so a new file needs somewhere
contiguous to land. That argues for keeping room for the largest file you
would write, not for a defragmentation figure. So a working disk keeps 10
percent, and one marked as cold storage, read from but not written to, is
allowed to fill to 97.
"""

import re

SSD = "ssd"
HDD = "hdd"
USB = "usb"
UNKNOWN = "unknown"

# A drive marked as cold storage is read from and not written to, so it can
# be filled almost completely. The remainder is for the filesystem, not for
# performance.
COLD_STORAGE_FREE_PCT = 3

# How much free space each type should keep, and why.
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
        "warn_free_pct": 10,
        "label": "USB",
        "reason": "Removable drives are usually a staging area rather than a home.",
    },
    UNKNOWN: {
        "warn_free_pct": 15,
        "label": "Unknown",
        "reason": "Type not known, so the cautious threshold is used. "
                  "Set the type to get the right one.",
    },
}

# Matches a type at the start of a volume label, e.g. "SSD-500GB-01",
# "HDD-4TB-02", "USB-32GB-03". The separator is optional so "SSD1" works too.
LABEL_PATTERN = re.compile(r"^\s*(ssd|hdd|usb|nvme|flash|thumb)\b[-_ ]?", re.IGNORECASE)

LABEL_ALIASES = {
    "ssd": SSD,
    "nvme": SSD,
    "hdd": HDD,
    "usb": USB,
    "flash": USB,
    "thumb": USB,
}


def from_label(label):
    """Work out the type from a volume label, or None if it does not say."""
    if not label:
        return None
    match = LABEL_PATTERN.match(label)
    if not match:
        return None
    return LABEL_ALIASES.get(match.group(1).lower())


def detect(label=None, removable=False, stored=None):
    """
    Decide a drive's type.

    stored wins, because it was set by hand. Otherwise the volume label is
    read, since the naming convention here already carries it. Failing that a
    removable volume is treated as USB. Anything else is unknown, which uses
    the cautious threshold rather than guessing.
    """
    if stored in (SSD, HDD, USB):
        return stored
    from_name = from_label(label)
    if from_name:
        return from_name
    if removable:
        return USB
    return UNKNOWN


def rule_for(drive_type):
    return SPACE_RULES.get(drive_type, SPACE_RULES[UNKNOWN])


def evaluate(drive_type, free_bytes, total_bytes, cold_storage=False):
    """
    Should this drive be flagged, and what should it say?

    Returns a dict with free_pct, threshold_pct, low (bool), severity
    ('ok', 'warn', 'critical') and a message. A cold storage drive is never
    flagged: it is full on purpose.
    """
    total = total_bytes or 0
    free_pct = (100.0 * (free_bytes or 0) / total) if total else 0.0
    rule = rule_for(drive_type)
    threshold = rule["warn_free_pct"]

    if cold_storage:
        # Read-only archive, so nearly all of it is usable. The small reserve
        # left is for the filesystem itself, not for performance.
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
