"""
scanner.py - walk a drive and record its tree into the database.

    py scanner.py D:\\ --label "Seagate 4TB Blue"
    py scanner.py D:\\ --redundancy-include "Desktop,Emulators"

Indexes top-level folders matching --root-prefix as the library, and any
containing --redundancy-prefix as backup copies. Inside a redundancy root
only recognisable media categories are walked, which keeps folders like
"Google Drive" out of the index.

Each scan replaces that drive's previous snapshot, so deletions and renames
are picked up. Tags are keyed by path in a separate table and survive.
"""

import argparse
import os
import shutil
import sys
import threading
import time

import db
import driveid
import tagging

DEFAULT_ROOT_PREFIX = "videos,00_videos"
DEFAULT_REDUNDANCY_PREFIX = "redundancy"

# Files Windows and macOS scatter through folders. Skipped by name as well as
# by attribute, because a drive reached over SMB or rclone reports no
# attributes at all.
JUNK_FILENAMES = {
    "desktop.ini", "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db",
    ".ds_store", "._.ds_store", "folder.ico", "autorun.inf",
}

FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04


def is_junk_name(name):
    return name.lower() in JUNK_FILENAMES


def is_junk_attrs(stat_result):
    """Hidden or system, so not media whatever it is called. Catches whatever
    Windows invents next; returns False where no attributes are reported."""
    attrs = getattr(stat_result, "st_file_attributes", 0)
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))

# The library layout MediaVault scaffolds onto a drive that has none.
DEFAULT_LIBRARY_ROOT = "Videos"
DEFAULT_CATEGORIES = ["Anime", "Anime Movies", "Movies", "TV Shows"]


def find_top_level_matches(drive_root, matches_fn):
    """Top-level folders on the drive for which matches_fn(name) is True."""
    found = []
    try:
        with os.scandir(drive_root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and matches_fn(entry.name):
                    found.append(entry.path)
    except OSError as e:
        print(f"Warning: couldn't list {drive_root}: {e}")
    return found


def is_numeric_prefixed(name):
    """Digits then an underscore, e.g. '00_Anime'. The convention that marks a
    folder as a deliberate media category."""
    for i, ch in enumerate(name):
        if ch == "_" and i > 0:
            return True
        if not ch.isdigit():
            return False
    return False


def looks_like_category(name):
    """
    Does this folder name look like a media category?

    Accepts both conventions, "00_Anime" and plain "Anime". The plain case
    reuses tagging.py's matching, so anything it can derive a tag from
    counts, which keeps "Desktop" and "Google Drive" out.
    """
    return is_numeric_prefixed(name) or bool(tagging.default_tags_for_category(name))


def find_redundancy_subfolders(redundancy_root, extra_includes, log=print):
    """Subfolders of a redundancy root worth scanning. extra_includes is a set
    of lowercase names to accept whatever they are called."""
    found = []
    try:
        with os.scandir(redundancy_root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if looks_like_category(entry.name) or entry.name.lower() in extra_includes:
                    found.append(entry.path)
                else:
                    log(f"  Skipping non-media subfolder inside redundancy: {entry.name}")
    except OSError as e:
        log(f"Warning: couldn't list {redundancy_root}: {e}")
    return found


def compute_dir_sizes(root_path):
    """Pass 1, bottom-up: recursive size of every directory under root_path.
    Returns {abs_path: size_bytes}."""
    sizes = {}
    for dirpath, dirnames, filenames in os.walk(root_path, topdown=False, onerror=lambda e: None):
        total = 0
        for fname in filenames:
            if is_junk_name(fname):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.islink(fpath):
                    continue
                stat = os.stat(fpath)
                # Skipped here as well as in build_nodes, so folder totals do
                # not count bytes that no node represents.
                if is_junk_attrs(stat):
                    continue
                total += stat.st_size
            except OSError:
                continue
        for dname in dirnames:
            total += sizes.get(os.path.join(dirpath, dname), 0)
        sizes[dirpath] = total
    return sizes


def build_nodes(drive_root, root_path, dir_sizes, next_id, root_type, allowed_subfolders=None):
    """
    Pass 2, top-down: flat node records in parent-before-child order, linked
    by tmp_id which db.replace_nodes() remaps to real row ids.

    allowed_subfolders, if given, restricts which direct children of
    root_path are walked. Returns (nodes, next_id).
    """
    nodes = []
    counter = [next_id]  # threaded through via a mutable list for nested calls

    def make_node(path, parent_tmp_id, depth, is_dir, size_override=None,
                  created_at=None, modified_at=None):
        tmp_id = counter[0]
        counter[0] += 1
        rel = os.path.relpath(path, drive_root)
        size = size_override if size_override is not None else dir_sizes.get(path, 0)
        record = {
            "tmp_id": tmp_id,
            "parent_tmp_id": parent_tmp_id,
            "name": os.path.basename(path),
            "rel_path": rel,
            "is_dir": is_dir,
            "size_bytes": size,
            "depth": depth,
            "root_type": root_type,
            "created_at": created_at,
            "modified_at": modified_at,
        }
        nodes.append(record)
        return tmp_id, record

    def walk(path, parent_tmp_id, depth, at_root=False):
        try:
            folder_created = os.stat(path).st_ctime
        except OSError:
            folder_created = None
        my_id, my_record = make_node(path, parent_tmp_id, depth, is_dir=True,
                                     created_at=folder_created)
        newest = None

        try:
            with os.scandir(path) as entries:
                entry_list = list(entries)
        except OSError:
            return newest

        for entry in entry_list:
            if entry.name.startswith(".") or entry.name == driveid.MARKER_NAME:
                continue
            # At the redundancy root level, skip non-approved subfolders.
            if at_root and allowed_subfolders is not None:
                if entry.is_dir(follow_symlinks=False) and entry.path not in allowed_subfolders:
                    continue
            if entry.is_dir(follow_symlinks=False):
                child_newest = walk(entry.path, my_id, depth + 1)
                if child_newest and (newest is None or child_newest > newest):
                    newest = child_newest
            elif entry.is_file():
                if is_junk_name(entry.name):
                    continue
                try:
                    stat = entry.stat()
                    if is_junk_attrs(stat):
                        continue
                    fsize, created, modified = stat.st_size, stat.st_ctime, stat.st_mtime
                except OSError:
                    fsize, created, modified = 0, None, None
                make_node(entry.path, my_id, depth + 1, is_dir=False, size_override=fsize,
                          created_at=created, modified_at=modified)
                # A downloaded file keeps its original mtime, so the moment it
                # landed here is the creation time. Take whichever is later, so
                # both a fresh download and an edited file count as recent.
                for stamp in (created, modified):
                    if stamp and (newest is None or stamp > newest):
                        newest = stamp

        my_record["modified_at"] = newest
        return newest

    walk(root_path, None, 0, at_root=(allowed_subfolders is not None))
    return nodes, counter[0]


def scan_drive(drive_root, root_prefix, redundancy_prefix, redundancy_include=None):
    """redundancy_include: set of lowercase folder names that are always
    included inside redundancy roots, in addition to the default numeric-
    prefixed ones (e.g. {"desktop", "emulators"})."""
    extra_includes = {n.strip().lower() for n in (redundancy_include or []) if n.strip()}

    # root_prefix can be comma-separated, e.g. "videos,00_videos"
    root_prefixes = [p.strip().lower() for p in root_prefix.split(",") if p.strip()]
    library_roots = find_top_level_matches(
        drive_root, lambda name: any(name.lower().startswith(p) for p in root_prefixes)
    )
    redundancy_roots = find_top_level_matches(
        drive_root, lambda name: redundancy_prefix.lower() in name.lower()
    )

    all_nodes = []
    next_id = 0
    for lib_root in library_roots:
        dir_sizes = compute_dir_sizes(lib_root)
        nodes, next_id = build_nodes(drive_root, lib_root, dir_sizes, next_id, root_type="library")
        all_nodes.extend(nodes)

    for red_root in redundancy_roots:
        # Only walk numeric-prefixed subfolders (e.g. 00_Anime, 01_Movies)
        # inside the redundancy root.
        subfolders = find_redundancy_subfolders(red_root, extra_includes)
        if not subfolders:
            print(f"  Warning: no recognizable category subfolders found in {red_root}. "
                  f"Use --redundancy-include to add non-standard folder names.")
            continue

        # Compute sizes for every node inside each approved subfolder and
        # merge into one map so build_nodes can look up sizes at any depth.
        combined_dir_sizes = {}
        for sf in subfolders:
            combined_dir_sizes.update(compute_dir_sizes(sf))

        # The redundancy root node itself gets the sum of its approved
        # subfolder sizes (non-approved folders are excluded from the total).
        combined_dir_sizes[red_root] = sum(
            combined_dir_sizes.get(sf, 0) for sf in subfolders
        )

        root_nodes, next_id = build_nodes(
            drive_root, red_root, combined_dir_sizes, next_id, root_type="redundancy",
            allowed_subfolders=set(subfolders),
        )
        all_nodes.extend(root_nodes)

    return all_nodes, library_roots, redundancy_roots


def seed_default_tags(drive_id, nodes):
    """Auto-tag every title from its category folder's name, but only where it
    has no tags at all yet, so hand-set tags are never overwritten."""
    by_tmp_id = {n["tmp_id"]: n for n in nodes}
    pairs = []
    for n in nodes:
        if n["depth"] == 2:
            parent = by_tmp_id.get(n["parent_tmp_id"])
            category_name = parent["name"] if parent else ""
            pairs.append((n["rel_path"], tagging.default_tags_for_category(category_name)))
    db.seed_default_tags_bulk(drive_id, pairs)


def resolve_label(drive_id, drive_root, label=None):
    """What to call this drive, best source first: explicit, remembered from a
    previous scan, volume label, drive letter. Returns (label, source)."""
    if label:
        return label, "provided"

    existing = {d["drive_id"]: d["label"] for d in db.get_all_drives()}
    remembered = existing.get(drive_id)
    if remembered:
        return remembered, "remembered from a previous scan"

    drive_letter = os.path.splitdrive(drive_root)[0]  # e.g. "D:"
    vol_label = driveid.get_drive_name(drive_root)
    if vol_label:
        auto = f"{vol_label} ({drive_letter})" if drive_letter else vol_label
    else:
        auto = drive_letter or drive_root
    return auto, "auto-detected - pass --label to set a custom name"


def remembered_scan_options(drive_id):
    """
    The options this drive was last scanned with, as kwargs for
    scan_and_store. Replaying them stops a rescan from the dashboard quietly
    dropping folders that were only indexed because of --redundancy-include.
    """
    drive = db.get_drive(drive_id)
    if not drive:
        return {}

    options = {}
    if drive.get("root_prefix"):
        options["root_prefix"] = drive["root_prefix"]
    if drive.get("redundancy_prefix") is not None:
        options["redundancy_prefix"] = drive["redundancy_prefix"]
    if drive.get("redundancy_include"):
        options["redundancy_include"] = [
            n for n in drive["redundancy_include"].split(",") if n
        ]
    return options


def scan_and_store(drive_path, label=None, root_prefix=DEFAULT_ROOT_PREFIX,
                   redundancy_prefix=DEFAULT_REDUNDANCY_PREFIX, redundancy_include=None,
                   log=None):
    """
    Scan one drive and write it to the database. The single entry point for
    both the CLI and the dashboard, so the two cannot drift apart.

    log takes one string per progress line. Returns a summary dict, or raises
    ValueError if the path is not a mounted directory.
    """
    log = log or print

    drive_root = os.path.abspath(drive_path)
    if not os.path.isdir(drive_root):
        raise ValueError(f"'{drive_root}' is not a directory or is not currently connected.")

    redundancy_include = redundancy_include or []
    prefixes_display = ", ".join(f"'{p.strip()}'" for p in root_prefix.split(",") if p.strip())
    include_display = (
        f", extra redundancy inclusions: {redundancy_include}" if redundancy_include else ""
    )
    log(f"Scanning {drive_root} "
        f"(library folders starting with {prefixes_display}, "
        f"redundancy folders containing '{redundancy_prefix}'{include_display}) ...")

    total, used, free = shutil.disk_usage(drive_root)
    drive_id = driveid.get_drive_id(drive_root, total)

    nodes, library_roots, redundancy_roots = scan_drive(
        drive_root, root_prefix, redundancy_prefix, redundancy_include
    )

    if not library_roots and not redundancy_roots:
        log(f"Warning: no top-level folder starting with {prefixes_display} "
            f"or containing '{redundancy_prefix}' found on this drive. Nothing indexed.")

    db.init_db()
    label, label_source = resolve_label(drive_id, drive_root, label)

    scan_options = {
        "root_prefix": root_prefix,
        "redundancy_prefix": redundancy_prefix,
        "redundancy_include": ",".join(redundancy_include),
    }
    db.upsert_drive(drive_id, label, total, used, free, drive_root, scan_options)
    db.replace_nodes(drive_id, nodes)
    seed_default_tags(drive_id, nodes)

    summary = {
        "drive_id": drive_id,
        "label": label,
        "label_source": label_source,
        "path": drive_root,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "library_roots": [os.path.basename(r) for r in library_roots],
        "redundancy_roots": [os.path.basename(r) for r in redundancy_roots],
        "dir_count": sum(1 for n in nodes if n["is_dir"]),
        "file_count": sum(1 for n in nodes if not n["is_dir"]),
    }

    log(f"Drive: {label}  [{label_source}]  (id: {drive_id})")
    log(f"Capacity: {used / 2**30:.1f} GB used / {total / 2**30:.1f} GB total "
        f"({free / 2**30:.1f} GB free)")
    log(f"Library folders: {summary['library_roots'] or 'none'}")
    log(f"Redundancy folders: {summary['redundancy_roots'] or 'none'}")
    log(f"Indexed {summary['dir_count']} folders, {summary['file_count']} files.")
    return summary


def list_mounted_roots():
    """Every currently mounted drive root, e.g. {'C:\\\\', 'D:\\\\', ...}."""
    if os.name != "nt":
        return set()
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return set()
    return {f"{chr(ord('A') + i)}:\\" for i in range(26) if mask & (1 << i)}


# A network drive whose far end has gone away keeps its letter, but every
# read against it blocks until Windows gives up, which is long enough to
# wedge the dashboard. So roots are probed on throwaway threads with a
# deadline, and one that misses it is remembered as absent: the stuck thread
# is not coming back, and without the cooldown every request would start
# another.
#
# The two timeouts differ because a local disk answers in milliseconds, while
# a sleeping share or a phone on power-save routinely takes seconds and is
# perfectly healthy.
PROBE_TIMEOUT_SECONDS = 2.5
NETWORK_PROBE_TIMEOUT_SECONDS = 10.0
UNRESPONSIVE_COOLDOWN_SECONDS = 30.0


def probe_timeout_for(root):
    """How long this root gets to answer, by what kind of drive it is."""
    return (NETWORK_PROBE_TIMEOUT_SECONDS
            if get_drive_type(root) == DRIVE_REMOTE
            else PROBE_TIMEOUT_SECONDS)


def ensure_readable(root):
    """
    Raise OSError if this drive cannot be listed at all.

    Needed because has_library_folders answers False both for a drive with no
    library and for one that could not be read, so without this a share whose
    host is off looks like an empty drive and gets offered for setup.
    """
    with os.scandir(root) as entries:
        next(iter(entries), None)

_unresponsive = {}


def _mark_unresponsive(root):
    _unresponsive[root] = time.monotonic()


def _clear_unresponsive(root):
    _unresponsive.pop(root, None)


def is_unresponsive(root):
    """Did this root recently fail to answer inside the timeout?"""
    failed_at = _unresponsive.get(root)
    return (failed_at is not None
            and time.monotonic() - failed_at < UNRESPONSIVE_COOLDOWN_SECONDS)


def unresponsive_roots():
    """Mounted roots that could not be read, so the UI can say so rather than
    silently dropping a drive that is plainly visible in Explorer."""
    return sorted(r for r in _unresponsive if is_unresponsive(r))


def probe_roots(roots, work, timeout=probe_timeout_for):
    """
    Run work(root) against every root at once, dropping any that hang.

    Threads start together, so the whole call costs one timeout rather than
    one per drive, and they are daemons so a stuck one cannot hold up exit.
    timeout is seconds, or a function of the root, which is what lets a
    network share have longer than a local disk in the same pass.
    """
    seconds_for = timeout if callable(timeout) else (lambda _root: timeout)

    results = {}
    finished = set()
    threads = []

    for root in roots:
        if is_unresponsive(root):
            continue

        def run(r=root):
            # An error is an answer, so raising still counts as finished. For
            # some probes failing is the ordinary case: reading the marker
            # file from a never-scanned drive is meant to fail, and calling
            # that dead would condemn every drive awaiting setup.
            try:
                results[r] = work(r)
            except OSError:
                pass
            finally:
                finished.add(r)

        thread = threading.Thread(target=run, daemon=True, name=f"probe-{root}")
        thread.start()
        threads.append((root, thread))

    # All measured from one start, since they all began together.
    started = time.monotonic()
    for root, thread in threads:
        deadline = started + seconds_for(root)
        thread.join(max(0.0, deadline - time.monotonic()))
        if root in finished:
            _clear_unresponsive(root)
        else:
            _mark_unresponsive(root)

    return results


CONNECTED_CACHE_SECONDS = 5.0

# Kept per include_network, because the two answers are genuinely different
# sets and one must never be served in place of the other.
_connected_cache = {
    True: {"checked_at": 0.0, "drives": {}},
    False: {"checked_at": 0.0, "drives": {}},
}


def get_connected_drives(max_age=CONNECTED_CACHE_SECONDS, include_network=True):
    """
    Which known drives are plugged in right now: {drive_id: root_path}.

    Reads the marker file from every mounted volume, since last_seen_path can
    be stale or the letter may now belong to a different drive. Cached for a
    few seconds because search calls this on every keystroke.

    include_network False leaves mapped drives untouched, so local drives do
    not wait on a sleeping host.
    """
    cache = _connected_cache[bool(include_network)]
    now = time.monotonic()
    if now - cache["checked_at"] < max_age:
        return cache["drives"]

    roots = list_mounted_roots()
    if not include_network:
        roots = {r for r in roots if get_drive_type(r) != DRIVE_REMOTE}

    def read_marker(root):
        with open(os.path.join(root, driveid.MARKER_NAME), "r") as f:
            return f.read().strip()

    found = {}
    for root, drive_id in probe_roots(roots, read_marker).items():
        if drive_id:
            found[drive_id] = root

    cache["checked_at"] = now
    cache["drives"] = found
    return found


def drive_letter_for(root_path):
    """'D:\\' -> 'D:'. Returns None if there is no letter to show."""
    if not root_path:
        return None
    return os.path.splitdrive(root_path)[0] or None


def find_mounted_drive(drive_id, max_age=CONNECTED_CACHE_SECONDS):
    """Where this drive is plugged in right now, or None if it is not."""
    return get_connected_drives(max_age).get(drive_id)


# GetDriveTypeW values. Network drives are scannable so a disk in another PC,
# or a mounted phone, can be indexed like any other. Identity still comes from
# the marker file, so it is the same drive whatever letter it lands on.
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
SCANNABLE_DRIVE_TYPES = {DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE}


def get_drive_type(root_path):
    """Windows drive type number, or None off Windows."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root_path))
    except Exception:
        return None


# STORAGE_BUS_TYPE values we care about. The full list is longer.
BUS_TYPE_USB = 7
BUS_TYPE_NAMES = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre",
                  7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA",
                  12: "SD", 13: "MMC", 17: "NVMe"}

_bus_cache = {}


def get_bus_type(root_path):
    """
    Which bus a drive is attached to, e.g. 'USB', 'SATA', 'NVMe', 'SD'.

    GetDriveType is not enough: Windows only calls a volume removable when
    the media itself can be swapped, so an external USB hard disk reports as
    fixed. Returns None off Windows or when the device will not answer.
    """
    if os.name != "nt" or not root_path:
        return None

    letter = os.path.splitdrive(root_path)[0].rstrip(":")
    if not letter:
        return None
    if letter in _bus_cache:
        return _bus_cache[letter]

    result = _query_bus_type(letter)
    _bus_cache[letter] = result
    return result


def _query_bus_type(letter):
    import ctypes
    from ctypes import wintypes

    IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [("PropertyId", wintypes.DWORD),
                    ("QueryType", wintypes.DWORD),
                    ("AdditionalParameters", ctypes.c_byte * 1)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p

    # Access 0 means "just ask about the device", which needs no admin rights.
    handle = kernel32.CreateFileW(f"\\\\.\\{letter}:", 0, FILE_SHARE_READ_WRITE,
                                  None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE:
        return None

    try:
        query = STORAGE_PROPERTY_QUERY()
        query.PropertyId = 0      # StorageDeviceProperty
        query.QueryType = 0       # PropertyStandardQuery

        buffer = ctypes.create_string_buffer(1024)
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            ctypes.c_void_p(handle), IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query), ctypes.sizeof(query),
            buffer, ctypes.sizeof(buffer), ctypes.byref(returned), None,
        )
        if not ok or returned.value < 32:
            return None
        # BusType sits at offset 28 of STORAGE_DEVICE_DESCRIPTOR.
        bus = int.from_bytes(buffer.raw[28:32], "little")
        return BUS_TYPE_NAMES.get(bus, f"bus {bus}")
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def is_external(root_path):
    """Can this drive be unplugged and stored somewhere else? True for the USB
    bus, including external hard disks Windows reports as fixed."""
    if get_bus_type(root_path) == "USB":
        return True
    return get_drive_type(root_path) == DRIVE_REMOVABLE


def has_library_folders(drive_root, root_prefix=DEFAULT_ROOT_PREFIX,
                        redundancy_prefix=DEFAULT_REDUNDANCY_PREFIX):
    """Does this drive have anything MediaVault would index?"""
    prefixes = [p.strip().lower() for p in root_prefix.split(",") if p.strip()]
    if find_top_level_matches(drive_root, lambda n: any(n.lower().startswith(p) for p in prefixes)):
        return True
    if redundancy_prefix and find_top_level_matches(
        drive_root, lambda n: redundancy_prefix.lower() in n.lower()
    ):
        return True
    return False


def find_scannable_drives():
    """
    Connected drives worth scanning: {root, drive_id, label, reason}.

    A drive qualifies if it is already known, or has a library or redundancy
    folder. Everything else is left alone, so scanning does not create empty
    entries for the system drive or a stick that happens to be plugged in.
    Only lettered volumes are visible to Windows here, not bare UNC paths.
    """
    known = {d["drive_id"]: d for d in db.get_all_drives()}
    connected = get_connected_drives(max_age=0)
    by_root = {root: drive_id for drive_id, root in connected.items()}

    found = []
    unknown_roots = []
    for root in sorted(list_mounted_roots()):
        drive_type = get_drive_type(root)
        if drive_type is not None and drive_type not in SCANNABLE_DRIVE_TYPES:
            continue

        drive_id = by_root.get(root)
        if drive_id and drive_id in known:
            found.append({"root": root, "drive_id": drive_id,
                          "label": known[drive_id]["label"], "reason": "known drive"})
            continue
        unknown_roots.append(root)

    def describe(root):
        ensure_readable(root)
        return {
            "has_library": has_library_folders(root),
            # No stored label yet, so the confirm prompt uses whatever the
            # volume calls itself rather than a bare drive letter.
            "label": driveid.get_drive_name(root),
        }

    for root, info in probe_roots(unknown_roots, describe).items():
        if info["has_library"]:
            found.append({"root": root, "drive_id": by_root.get(root),
                          "label": info["label"],
                          "reason": "has a library folder"})

    return found


def deferred_network_roots():
    """
    Mounted network drives, named, without touching the network. The share
    name comes from the local redirector's table, so it is instant whether or
    not the far end is awake.
    """
    out = []
    for root in sorted(list_mounted_roots()):
        if get_drive_type(root) != DRIVE_REMOTE:
            continue
        out.append({
            "root": root,
            "letter": drive_letter_for(root) or root,
            "name": driveid.get_share_name(root) or "(network drive)",
        })
    return out


def find_setup_candidates(include_network=True):
    """
    Connected drives that do not have the folder layout yet, for the setup
    panel. Looks at mounted volumes rather than known drives, because a drive
    with no library folder is never scanned and so is not in the database.

    include_network False leaves network drives untouched; the dashboard
    lists them from deferred_network_roots() and checks them on request.
    """
    known = {d["drive_id"] for d in db.get_all_drives()}
    connected = get_connected_drives(max_age=0, include_network=include_network)
    by_root = {root: drive_id for drive_id, root in connected.items()}

    # get_drive_type is answered from memory and cannot block, so it narrows
    # the set before anything touches a volume.
    types = {}
    for root in sorted(list_mounted_roots()):
        drive_type = get_drive_type(root)
        if drive_type is not None and drive_type not in SCANNABLE_DRIVE_TYPES:
            continue
        if drive_type == DRIVE_REMOTE and not include_network:
            continue
        types[root] = drive_type

    # Both touch the volume, so they share one probe thread and one timeout.
    def describe(root):
        ensure_readable(root)
        return {
            "has_library": has_library_folders(root),
            "name": driveid.get_drive_name(root),
        }

    described = probe_roots(types, describe)

    candidates = []
    for root, drive_type in types.items():
        info = described.get(root)
        # Absent means it never answered, and scaffolding onto a drive that
        # cannot be read would only fail later.
        if info is None or info["has_library"]:
            continue

        drive_id = by_root.get(root)
        # Letter and name stay apart so the UI can column them.
        candidates.append({
            "root": root,
            "drive_id": drive_id,
            "letter": drive_letter_for(root) or root,
            "name": info["name"] or "(no volume label)",
            "known": drive_id in known if drive_id else False,
            "removable": drive_type == DRIVE_REMOVABLE,
        })
    return candidates


def create_library_structure(drive_root, root_name=DEFAULT_LIBRARY_ROOT,
                             categories=None):
    """
    Create the Videos/<category> tree on a drive that has none. Safe on a
    drive that is partway set up, since existing folders are left alone.
    Returns the relative paths actually created, which may be empty.
    """
    categories = categories or DEFAULT_CATEGORIES
    created = []
    lib_root = os.path.join(drive_root, root_name)
    for category in [None] + list(categories):
        path = lib_root if category is None else os.path.join(lib_root, category)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            created.append(os.path.relpath(path, drive_root))
    return created


def main():
    parser = argparse.ArgumentParser(description="Scan a drive into MediaVault.")
    parser.add_argument("drive_path", help=r"Root of the drive to scan, e.g. D:\ or D:")
    parser.add_argument("--label", help="Human-readable name for this drive (e.g. 'Seagate 4TB Blue')")
    parser.add_argument(
        "--root-prefix",
        default=DEFAULT_ROOT_PREFIX,
        help="Case-insensitive comma-separated prefixes for top-level library folders to scan "
             "(default: 'videos,00_videos', matches 'Videos', 'Videos_SSD1', '00_Videos', "
             "'00_Videos_SSD1', etc.). Pass a single prefix to override, or multiple "
             "comma-separated prefixes.",
    )
    parser.add_argument(
        "--redundancy-prefix",
        default=DEFAULT_REDUNDANCY_PREFIX,
        help="Case-insensitive substring for top-level backup/redundancy folders (default: 'redundancy', "
             "matches '99_Redundancy', 'Redundancy_Backup', etc.). Pass '' to disable.",
    )
    parser.add_argument(
        "--redundancy-include",
        default="",
        help="Comma-separated list of subfolder names (exact, case-insensitive) inside the redundancy "
             "root that should be scanned IN ADDITION to the default numeric-prefixed folders "
             "(e.g. '00_Anime', '01_Movies'). Example: --redundancy-include \"Desktop,Emulators\". ",
    )
    args = parser.parse_args()

    print("  Note: inside redundancy folders, only subfolders that look like media "
          "categories\n  (Anime, Movies, TV Shows, or the numbered 00_Anime form) "
          "are scanned by default.")

    try:
        scan_and_store(
            args.drive_path,
            label=args.label,
            root_prefix=args.root_prefix,
            redundancy_prefix=args.redundancy_prefix,
            redundancy_include=[n.strip() for n in args.redundancy_include.split(",") if n.strip()],
        )
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("Done. Open the dashboard to view (py dashboard.py).")


if __name__ == "__main__":
    main()
