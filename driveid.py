"""
driveid.py - a stable identity for every drive.

Drive letters move, so the database cannot key on them. The first scan
writes a hidden marker file holding a UUID at the drive root, and every
later scan reads it back.

A drive that cannot be written to falls back to a fingerprint of its volume
serial number, label and size. The serial is set at format time and is the
nearest thing to a hardware id available without admin rights.
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


def get_share_name(drive_root):
    """
    The share behind a mapped network drive, e.g. "HDD-4TB-02" for a Z:
    mapped to \\\\FILESERVER\\HDD-4TB-02. Also accepts a UNC path directly.

    Worth having because most SMB shares report no volume label at all, so a
    network drive would otherwise be nameless.
    """
    if os.name != "nt" or not drive_root:
        return None

    unc = _unc_for(drive_root)
    if not unc:
        return None
    # "\\\\server\\share" -> "share"; a bare "\\\\server" has nothing to give.
    parts = unc.strip("\\").split("\\")
    return parts[-1] if len(parts) >= 2 else None


def _unc_for(drive_root):
    """The UNC path a root refers to, or None if it is a local volume."""
    prefix = os.path.splitdrive(drive_root)[0]
    if prefix.startswith("\\\\"):
        return prefix
    if not prefix.endswith(":"):
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        length = ctypes.c_ulong(ctypes.sizeof(buf) // ctypes.sizeof(ctypes.c_wchar))
        rc = ctypes.windll.mpr.WNetGetConnectionW(
            ctypes.c_wchar_p(prefix), buf, ctypes.byref(length),
        )
        # Anything else means the letter is local, disconnected, or unknown.
        return buf.value or None if rc == 0 else None
    except Exception:
        return None


def get_drive_name(drive_root):
    """
    What to call this drive: the share name if mapped over the network,
    otherwise the volume label.

    The share wins because it is the name this machine gave the mapping, and
    so the one the naming convention is written on. The label underneath is
    whatever the other machine calls the disk.
    """
    return get_share_name(drive_root) or get_volume_info(drive_root)[0]


def _fallback_fingerprint(drive_root, total_bytes):
    """
    Stable id for a drive that cannot take a marker file. The volume serial
    is unique per format and survives relettering; label and size are extra
    entropy, and the only signal at all when the serial cannot be read.
    """
    label, serial = get_volume_info(drive_root)
    if not label:
        # normpath("C:\\") basenames to "", which would contribute nothing.
        label = (_unc_for(drive_root)
                 or os.path.basename(os.path.normpath(drive_root))
                 or str(drive_root))
    raw = f"{serial or 'noserial'}-{label}-{total_bytes}"
    return "ro-" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def identify(drive_root, total_bytes):
    """
    This drive's id without creating anything: its marker file if it has one,
    otherwise the same fingerprint get_drive_id falls back to.

    The connected-drive probe needs this. Reading only the marker meant a
    share that can never take one, such as another machine's system drive
    mounted read only, was reported as unplugged no matter how healthy it was.
    """
    try:
        with open(os.path.join(drive_root, MARKER_NAME), "r") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    return _fallback_fingerprint(drive_root, total_bytes)


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
