"""
driveid.py - Give every physical drive a stable identity.

Drive letters (E:, F:...) and mount points change depending on what else is
plugged in, so we can't key the database on those. Instead, the first time
a drive is scanned we write a small hidden marker file at its root
containing a UUID. Every future scan reads that same file, so the drive is
recognized as "the same drive" no matter what letter Windows assigns it.

If the drive is read-only (can't write the marker), we fall back to a
fingerprint derived from the volume's NTFS serial number, its label, and
its total size. The serial number is assigned at format time and is the
closest thing to a hardware id available without admin rights, so two
different drives of the same capacity no longer collide.
"""

import os
import uuid
import hashlib
import ctypes

MARKER_NAME = ".mediavault_id"


def _hide_file_windows(path):
    if os.name == "nt":
        FILE_ATTRIBUTE_HIDDEN = 0x02
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass


def get_volume_info(drive_root):
    """
    Reads the native Windows volume label and serial number for a drive.

    Returns (label, serial) where label is e.g. the "HDD" in "HDD (D:)" and
    serial is the volume serial number assigned at format time (an int), or
    (None, None) off Windows / on failure.
    """
    if os.name != "nt":
        return None, None
    try:
        label_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_root), label_buf, ctypes.sizeof(label_buf),
            ctypes.byref(serial), None, None, fs_buf, ctypes.sizeof(fs_buf),
        )
        if not ok:
            return None, None
        return (label_buf.value or None), (serial.value or None)
    except Exception:
        return None, None


def get_windows_volume_label(drive_root):
    """Just the volume label - see get_volume_info()."""
    return get_volume_info(drive_root)[0]


def _fallback_fingerprint(drive_root, total_bytes):
    """
    Stable id for drives we can't write a marker file to (read-only media,
    permission-locked system volumes).

    Built from the volume serial number first - that is unique per formatted
    volume and survives relettering. Label and size are folded in as
    additional entropy, and as the only signal available if the serial can't
    be read (non-Windows, or an odd filesystem).
    """
    label, serial = get_volume_info(drive_root)
    if not label:
        # normpath("C:\\") basenames to "" - fall back to the raw root string
        # rather than silently contributing nothing to the fingerprint.
        label = os.path.basename(os.path.normpath(drive_root)) or str(drive_root)
    raw = f"{serial or 'noserial'}-{label}-{total_bytes}"
    return "ro-" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def get_drive_id(drive_root, total_bytes):
    marker_path = os.path.join(drive_root, MARKER_NAME)

    if os.path.exists(marker_path):
        try:
            with open(marker_path, "r") as f:
                existing = f.read().strip()
            if existing:
                return existing
        except OSError:
            pass

    new_id = str(uuid.uuid4())
    try:
        with open(marker_path, "w") as f:
            f.write(new_id)
        _hide_file_windows(marker_path)
        return new_id
    except OSError:
        # Read-only drive (or permissions issue) - use a stable fingerprint instead
        return _fallback_fingerprint(drive_root, total_bytes)
