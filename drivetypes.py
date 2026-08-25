"""
drivetypes.py - what kind of drive is this, and how full should it get?

The type matters because the sensible fill level differs. An SSD kept close
to full has less spare area to work with, so writes slow down and wear
levelling has less room to move; leaving headroom is worth it. A mechanical
disk has no such problem, but its outermost tracks are the slowest and a
nearly full volume fragments badly, so it is fine to fill one right up as
long as you then treat it as an archive you read from rather than write to.
"""

import re

SSD = "ssd"
HDD = "hdd"
USB = "usb"
UNKNOWN = "unknown"

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
        "reason": "Fine to fill, but writes get slow and fragmented near the end. "
                  "Either move something off, or mark it as cold storage and stop "
                  "writing to it.",
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

# Matches a type at the start of a volume label, e.g. "SSD-1TB-02",
# "HDD-2TB-01", "USB-8GB-DEL". The separator is optional so "SSD1" works too.
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


def warn_threshold(drive_type):
    """Free space fraction below which the drive should be flagged."""
    return rule_for(drive_type)["warn_free_pct"] / 100.0


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
        return {
            "free_pct": round(free_pct, 1),
            "threshold_pct": threshold,
            "low": False,
            "severity": "cold",
            "message": "Cold storage, kept full on purpose and not written to.",
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
