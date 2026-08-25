"""
scanner.py - Scan a drive and record it into the MediaVault database.

USAGE
    py scanner.py D:\\ --label "Seagate 4TB Blue"
    py scanner.py D:\\                                (label optional after first scan)
    py scanner.py D:\\ --root-prefix videos             (default - matches "Videos", "Videos_SSD1", etc.)
    py scanner.py D:\\ --redundancy-prefix redundancy    (default - matches "99_Redundancy", etc.)
    py scanner.py D:\\ --redundancy-include "Desktop,Emulators"  (also scan these named subfolders inside redundancy)

WHAT IT DOES
    1. Reads the drive's total/used/free capacity.
    2. Gets (or creates) a stable ID for the drive, independent of its
       drive letter, via driveid.py.
    3. Finds every top-level folder on the drive (root dirs)
    4. Also finds top-level folders whose name CONTAINS --redundancy-prefix
       (default "redundancy", matching e.g. "99_Redundancy") and scans them
       the same way, tagged as backup/redundancy copies rather than primary
       library folders.
       IMPORTANT: inside a redundancy root only subfolders whose names start
       with a numeric prefix (e.g. "00_Anime", "01_Movies", "02_TV Shows")
       are scanned by default. Add extra inclusions with
       --redundancy-include "FolderA,FolderB" (exact names, case-insensitive).
    5. Recursively walks everything inside those folders and records the
       FULL tree
    6. The first time a title (a folder directly inside a category folder,
       e.g. "Videos/00_Anime/<title>") is seen, it's auto-tagged based on
       its category folder. Tags you add, remove, or change from the dashboard
       afterwards are never overwritten by a rescan.
    7. Writes everything to mediavault.db, replacing this drive's previous
       snapshot so deletions/renames/moves are reflected accurately.

Run this after you add, move, or delete files on a drive. Nothing else
needs to be updated by hand.
"""

import argparse
import os
import shutil
import sys
import time

import db
import driveid
import tagging

DEFAULT_ROOT_PREFIX = "videos,00_videos"
DEFAULT_REDUNDANCY_PREFIX = "redundancy"

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
    """Returns True if the folder name starts with one or more digits followed
    by an underscore - e.g. '00_Anime', '01_Movies', '02_TV Shows', '99_Redundancy'.
    This is the convention used to mark intentional media-category folders."""
    for i, ch in enumerate(name):
        if ch == "_" and i > 0:
            return True
        if not ch.isdigit():
            return False
    return False


def looks_like_category(name):
    """
    Is this folder name a media category we'd expect inside a library?

    Accepts both naming conventions: the numeric-prefixed one ("00_Anime",
    "01_Movies") and the plain one ("Anime", "Movies", "TV Shows"). The plain
    case reuses tagging.py's category matching, so anything it can derive a
    tag from counts - which keeps non-media folders that happen to sit at the
    same level ("Desktop", "Google Drive") out.
    """
    return is_numeric_prefixed(name) or bool(tagging.default_tags_for_category(name))


def find_redundancy_subfolders(redundancy_root, extra_includes, log=print):
    """Returns the list of subfolders inside a redundancy root that should be
    scanned. Only recognizable media categories are included by default (see
    looks_like_category). extra_includes is a set of lowercase folder names
    that should additionally be included regardless of naming convention."""
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
    """
    Pass 1 (bottom-up): compute the recursive size of every directory under
    root_path (inclusive). Returns a dict {abs_path: size_bytes}.
    """
    sizes = {}
    for dirpath, dirnames, filenames in os.walk(root_path, topdown=False, onerror=lambda e: None):
        total = 0
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                if not os.path.islink(fpath):
                    total += os.path.getsize(fpath)
            except OSError:
                continue
        for dname in dirnames:
            total += sizes.get(os.path.join(dirpath, dname), 0)
        sizes[dirpath] = total
    return sizes


def build_nodes(drive_root, root_path, dir_sizes, next_id, root_type, allowed_subfolders=None):
    """
    Pass 2 (top-down): walk the same tree again, this time emitting flat
    node records in parent-before-child order, with tmp_id/parent_tmp_id
    links that db.replace_nodes() will remap to real row ids.
    Returns (list_of_nodes, next_id).

    allowed_subfolders: if not None, only these direct child paths of
    root_path are walked (used to restrict redundancy roots to numeric-
    prefixed category folders only).
    """
    nodes = []
    counter = [next_id]  # threaded through via a mutable list for nested calls

    def make_node(path, parent_tmp_id, depth, is_dir, size_override=None):
        tmp_id = counter[0]
        counter[0] += 1
        rel = os.path.relpath(path, drive_root)
        size = size_override if size_override is not None else dir_sizes.get(path, 0)
        nodes.append({
            "tmp_id": tmp_id,
            "parent_tmp_id": parent_tmp_id,
            "name": os.path.basename(path),
            "rel_path": rel,
            "is_dir": is_dir,
            "size_bytes": size,
            "depth": depth,
            "root_type": root_type,
        })
        return tmp_id

    def walk(path, parent_tmp_id, depth, at_root=False):
        my_id = make_node(path, parent_tmp_id, depth, is_dir=True)
        try:
            with os.scandir(path) as entries:
                entry_list = list(entries)
        except OSError:
            return
        for entry in entry_list:
            if entry.name.startswith(".") or entry.name == driveid.MARKER_NAME:
                continue
            # At the redundancy root level, skip non-approved subfolders.
            if at_root and allowed_subfolders is not None:
                if entry.is_dir(follow_symlinks=False) and entry.path not in allowed_subfolders:
                    continue
            if entry.is_dir(follow_symlinks=False):
                walk(entry.path, my_id, depth + 1)
            elif entry.is_file():
                try:
                    fsize = entry.stat().st_size
                except OSError:
                    fsize = 0
                make_node(entry.path, my_id, depth + 1, is_dir=False, size_override=fsize)

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
    """For every title (depth==2 dir or file), auto-tag it based on its
    category folder's name - but only if it has no tags yet
    (see db.seed_default_tags_bulk).
    Depth-2 files are movie/episode files sitting directly inside a category
    folder (e.g. 01_Movies/SomeMovie.mkv) and should also be auto-tagged."""
    by_tmp_id = {n["tmp_id"]: n for n in nodes}
    pairs = []
    for n in nodes:
        if n["depth"] == 2:
            parent = by_tmp_id.get(n["parent_tmp_id"])
            category_name = parent["name"] if parent else ""
            pairs.append((n["rel_path"], tagging.default_tags_for_category(category_name)))
    db.seed_default_tags_bulk(drive_id, pairs)


def resolve_label(drive_id, drive_root, label=None):
    """
    Works out what to call this drive: an explicit label wins, then whatever
    it was called on a previous scan, then the Windows volume label, then
    just the drive letter. Returns (label, how_we_got_it).
    """
    if label:
        return label, "provided"

    existing = {d["drive_id"]: d["label"] for d in db.get_all_drives()}
    remembered = existing.get(drive_id)
    if remembered:
        return remembered, "remembered from a previous scan"

    drive_letter = os.path.splitdrive(drive_root)[0]  # e.g. "D:"
    vol_label = driveid.get_windows_volume_label(drive_root)
    if vol_label:
        auto = f"{vol_label} ({drive_letter})" if drive_letter else vol_label
    else:
        auto = drive_letter or drive_root
    return auto, "auto-detected - pass --label to set a custom name"


def remembered_scan_options(drive_id):
    """
    The scan options this drive was last scanned with, as a dict of keyword
    arguments for scan_and_store. Empty if the drive is new or predates the
    options being stored.

    This is what stops a rescan started from the dashboard from silently
    falling back to the defaults and dropping folders that were only indexed
    because of a --redundancy-include.
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
    Scan one drive and write it to the database. This is the single entry
    point used by the CLI below, by watch_drives.py, and by the dashboard's
    scan endpoint - so all three behave identically.

    log: optional callable taking one string, for progress output. Defaults
    to print() for CLI use; the web endpoint passes a collector instead.

    Returns a summary dict. Raises ValueError if the path isn't a mounted
    directory, so callers can report it however suits them.
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


_connected_cache = {"checked_at": 0.0, "drives": {}}
CONNECTED_CACHE_SECONDS = 5.0


def get_connected_drives(max_age=CONNECTED_CACHE_SECONDS):
    """
    Which known drives are plugged in right now: {drive_id: root_path}.

    Reads the marker file from every mounted volume. That is the only
    reliable answer, because last_seen_path from the previous scan can be
    stale, or the letter may now belong to a different drive entirely.

    Results are cached for a few seconds. Search calls this on every
    keystroke, and hitting all 26 possible drive letters each time would
    spin up sleeping external drives for no reason.
    """
    now = time.monotonic()
    if now - _connected_cache["checked_at"] < max_age:
        return _connected_cache["drives"]

    found = {}
    for root in list_mounted_roots():
        marker = os.path.join(root, driveid.MARKER_NAME)
        try:
            with open(marker, "r") as f:
                drive_id = f.read().strip()
            if drive_id:
                found[drive_id] = root
        except OSError:
            continue

    _connected_cache["checked_at"] = now
    _connected_cache["drives"] = found
    return found


def drive_letter_for(root_path):
    """'D:\\' -> 'D:'. Returns None if there is no letter to show."""
    if not root_path:
        return None
    return os.path.splitdrive(root_path)[0] or None


def find_mounted_drive(drive_id, max_age=CONNECTED_CACHE_SECONDS):
    """Where this drive is plugged in right now, or None if it is not."""
    return get_connected_drives(max_age).get(drive_id)


# GetDriveTypeW return values we are willing to scan.
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
SCANNABLE_DRIVE_TYPES = {DRIVE_REMOVABLE, DRIVE_FIXED}


def get_drive_type(root_path):
    """Windows drive type number, or None off Windows. 4 is a network share,
    5 a CD-ROM, both of which we skip."""
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
    Which bus a drive is attached to, e.g. 'USB', 'SATA', 'NVMe'.

    GetDriveType is not enough here. Windows only calls a volume "removable"
    when the media itself can be swapped, as with a card reader or a flash
    drive, so an external USB hard disk reports as a fixed drive like any
    internal one. Asking the device for its bus type is the only way to tell
    that it is plugged in over USB and can be unplugged and carried away.

    Returns None off Windows, or when the device will not answer.
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
    """
    Can this drive be unplugged and stored somewhere else?

    True for anything on the USB bus, including external hard disks that
    Windows otherwise reports as fixed, and for media Windows does call
    removable.
    """
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
    Connected drives worth scanning, as a list of dicts with root, drive_id
    (None if unknown), and reason.

    A drive qualifies if MediaVault already knows it, or if it has a library
    or redundancy folder on it. Everything else is left alone, so scanning
    does not create empty entries for the system drive or for a USB stick
    that happens to be plugged in. Network drives and optical drives are
    skipped outright.
    """
    known = {d["drive_id"]: d for d in db.get_all_drives()}
    connected = get_connected_drives(max_age=0)
    by_root = {root: drive_id for drive_id, root in connected.items()}

    found = []
    for root in sorted(list_mounted_roots()):
        drive_type = get_drive_type(root)
        if drive_type is not None and drive_type not in SCANNABLE_DRIVE_TYPES:
            continue

        drive_id = by_root.get(root)
        if drive_id and drive_id in known:
            found.append({"root": root, "drive_id": drive_id,
                          "label": known[drive_id]["label"], "reason": "known drive"})
            continue

        try:
            if has_library_folders(root):
                found.append({"root": root, "drive_id": drive_id,
                              "label": None, "reason": "has a library folder"})
        except OSError:
            continue

    return found


def find_setup_candidates():
    """
    Connected drives that do NOT have the folder layout yet.

    These are the drives the dashboard can offer to set up. A drive with no
    library folder is never scanned, so it does not appear in the database at
    all, which is why this looks at mounted volumes rather than known drives.
    """
    known = {d["drive_id"] for d in db.get_all_drives()}
    connected = get_connected_drives(max_age=0)
    by_root = {root: drive_id for drive_id, root in connected.items()}

    candidates = []
    for root in sorted(list_mounted_roots()):
        drive_type = get_drive_type(root)
        if drive_type is not None and drive_type not in SCANNABLE_DRIVE_TYPES:
            continue
        try:
            if has_library_folders(root):
                continue
        except OSError:
            continue

        drive_id = by_root.get(root)
        volume_label, _serial = driveid.get_volume_info(root)
        # Letter and name are kept apart so the UI can lay them out in
        # columns. Joining them here is what made the letter appear twice.
        candidates.append({
            "root": root,
            "drive_id": drive_id,
            "letter": drive_letter_for(root) or root,
            "name": volume_label or "(no volume label)",
            "known": drive_id in known if drive_id else False,
            "removable": drive_type == DRIVE_REMOVABLE,
        })
    return candidates


def create_library_structure(drive_root, root_name=DEFAULT_LIBRARY_ROOT,
                             categories=None):
    """
    Create the expected Videos/<category> tree on a drive that has none.

    Never touches or overwrites anything that already exists - os.makedirs
    with exist_ok means an existing folder is simply left alone, so this is
    safe to run against a drive that's partway set up. Returns the list of
    folders it actually created (relative paths), which may be empty.
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
