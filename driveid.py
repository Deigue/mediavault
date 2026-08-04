"""
driveid.py - Give every physical drive a stable identity.

Drive letters (E:, F:...) and mount points change depending on what else is
plugged in, so we can't key the database on those. Instead, the first time
a drive is scanned we write a small hidden marker file at its root
containing a UUID. Every future scan reads that same file, so the drive is
recognized as "the same drive" no matter what letter Windows assigns it.

If the drive is read-only (can't write the marker), we fall back to a
fingerprint derived from its total size + volume label, which is stable
enough for read-only/archival media.
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


def _fallback_fingerprint(drive_root, total_bytes):
    try:
        vol_label = os.path.basename(os.path.normpath(drive_root))
    except Exception:
        vol_label = "unknown"
    raw = f"{vol_label}-{total_bytes}"
    return "ro-" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def get_windows_volume_label(drive_root):
    """Reads the native Windows volume label (the name you see in Explorer,
    e.g. the "HDD" in "HDD (D:)"), if available. Returns None elsewhere."""
    if os.name != "nt":
        return None
    try:
        label_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_root), label_buf, ctypes.sizeof(label_buf),
            None, None, None, fs_buf, ctypes.sizeof(fs_buf),
        )
        if ok and label_buf.value:
            return label_buf.value
    except Exception:
        pass
    return None


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
