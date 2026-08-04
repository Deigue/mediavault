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

import db
import driveid
import tagging


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


def find_redundancy_subfolders(redundancy_root, extra_includes):
    """Returns the list of subfolders inside a redundancy root that should be
    scanned. Only numeric-prefixed folders (00_Anime, 01_Movies, ...) are
    included by default. extra_includes is a set of lowercase folder names
    that should additionally be included regardless of naming convention."""
    found = []
    try:
        with os.scandir(redundancy_root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if is_numeric_prefixed(entry.name) or entry.name.lower() in extra_includes:
                    found.append(entry.path)
                else:
                    print(f"  Skipping non-media subfolder inside redundancy: {entry.name}")
    except OSError as e:
        print(f"Warning: couldn't list {redundancy_root}: {e}")
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
            print(f"  Warning: no numeric-prefixed subfolders found in {red_root}. "
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
    (see db.ensure_default_tags).
    Depth-2 files are movie/episode files sitting directly inside a category
    folder (e.g. 01_Movies/SomeMovie.mkv) and should also be auto-tagged."""
    by_tmp_id = {n["tmp_id"]: n for n in nodes}
    for n in nodes:
        if n["depth"] == 2:
            parent = by_tmp_id.get(n["parent_tmp_id"])
            category_name = parent["name"] if parent else ""
            defaults = tagging.default_tags_for_category(category_name)
            db.ensure_default_tags(drive_id, n["rel_path"], defaults)


def main():
    parser = argparse.ArgumentParser(description="Scan a drive into MediaVault.")
    parser.add_argument("drive_path", help=r"Root of the drive to scan, e.g. D:\ or D:")
    parser.add_argument("--label", help="Human-readable name for this drive (e.g. 'Seagate 4TB Blue')")
    parser.add_argument(
        "--root-prefix",
        default="videos,00_videos",
        help="Case-insensitive comma-separated prefixes for top-level library folders to scan "
             "(default: 'videos,00_videos', matches 'Videos', 'Videos_SSD1', '00_Videos', "
             "'00_Videos_SSD1', etc.). Pass a single prefix to override, or multiple "
             "comma-separated prefixes.",
    )
    parser.add_argument(
        "--redundancy-prefix",
        default="redundancy",
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

    drive_root = os.path.abspath(args.drive_path)
    if not os.path.isdir(drive_root):
        print(f"Error: '{drive_root}' is not a directory or is not currently connected.")
        sys.exit(1)

    redundancy_include = [n.strip() for n in args.redundancy_include.split(",") if n.strip()]

    prefixes_display = ", ".join(f"'{p.strip()}'" for p in args.root_prefix.split(",") if p.strip())
    include_display = f", extra redundancy inclusions: {redundancy_include}" if redundancy_include else ""
    print(f"Scanning {drive_root} "
          f"(library folders starting with {prefixes_display}, "
          f"redundancy folders containing '{args.redundancy_prefix}'{include_display}) ...")
    print("  Note: inside redundancy folders, only numeric-prefixed subfolders "
          "(e.g. 00_Anime, 01_Movies) are scanned by default.")

    total, used, free = shutil.disk_usage(drive_root)
    drive_id = driveid.get_drive_id(drive_root, total)

    nodes, library_roots, redundancy_roots = scan_drive(
        drive_root, args.root_prefix, args.redundancy_prefix, redundancy_include
    )

    if not library_roots and not redundancy_roots:
        print(f"Warning: no top-level folder starting with {prefixes_display} "
              f"or containing '{args.redundancy_prefix}' found on this drive.")
        print("Nothing indexed. Use --root-prefix / --redundancy-prefix to match your naming.")

    db.init_db()

    label = args.label
    label_source = "provided"
    if not label:
        existing = {d["drive_id"]: d["label"] for d in db.get_all_drives()}
        label = existing.get(drive_id)
        label_source = "remembered from a previous scan"
        if not label:
            drive_letter = os.path.splitdrive(drive_root)[0]  # e.g. "D:"
            vol_label = driveid.get_windows_volume_label(drive_root)
            if vol_label:
                label = f"{vol_label} ({drive_letter})" if drive_letter else vol_label
            else:
                label = drive_letter or drive_root
            label_source = "auto-detected - pass --label to set a custom name"

    db.upsert_drive(drive_id, label, total, used, free, drive_root)
    db.replace_nodes(drive_id, nodes)
    seed_default_tags(drive_id, nodes)

    print(f"Drive: {label}  [{label_source}]  (id: {drive_id})")
    print(f"Capacity: {used / 2**30:.1f} GB used / {total / 2**30:.1f} GB total "
          f"({free / 2**30:.1f} GB free)")
    print(f"Library folders: {[os.path.basename(r) for r in library_roots] or 'none'}")
    print(f"Redundancy folders: {[os.path.basename(r) for r in redundancy_roots] or 'none'}")
    print(f"Indexed {sum(1 for n in nodes if n['is_dir'])} folders, "
          f"{sum(1 for n in nodes if not n['is_dir'])} files.")
    print("Done. Open the dashboard to view (py dashboard.py).")


if __name__ == "__main__":
    main()
