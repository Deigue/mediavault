"""
migrate_categories.py - one-off tool to drop numeric prefixes from folder names.

ONLY NEEDED IF your folders use the old numbered convention. If your drives
already look like Videos/Anime, you do not need this script at all. Delete it
once every drive has been migrated.

It renames two kinds of folder:

    library roots       Videos_SSD3, Videos-SSD1, 00_Videos  ->  Videos
    category folders    00_Anime, 01_Movies, 02_TV Shows     ->  Anime, Movies, TV Shows

Category folders inside a redundancy root (99_Redundancy) are renamed too.
The redundancy root itself keeps its name.

USAGE

    py scripts/migrate_categories.py d              dry run on D:
    py scripts/migrate_categories.py c d e          dry run on C:, D: and E:
    py scripts/migrate_categories.py d --apply      rename, asking y/n for each folder
    py scripts/migrate_categories.py d --apply --yes    rename everything, no prompts

Drives can be given as a bare letter (d), a letter with a colon (d:), or a
full path (D:\\). Bare letters are the easy way.

OPTIONS

    --apply             actually rename. Without it nothing is modified.
    --yes               skip the y/n prompts and accept every rename.
    --keep-root-names   leave library roots alone, only rename categories.
    --keep-tags         keep existing tag rows instead of clearing them.

CONFIRMATION

With --apply, each rename is confirmed on its own, grouped under the folder
it belongs to. Answer y or n per folder, a to accept the rest of that drive,
or q to stop there.

TAGS

Tags are stored against a folder's path, so renaming a folder leaves its old
tags pointing at a path that no longer exists. After a successful rename the
script clears that drive's tags and rescans, which puts back the automatic
category tags (everything under Anime gets tagged Anime, and so on). Any tags
you added by hand on that drive are lost. Pass --keep-tags to keep the old
rows and sort them out yourself.

SAFETY

    Dry run is the default. Nothing changes without --apply.
    A rename is skipped if the target name already exists, so a part-finished
    migration can be re-run safely.
    Only the folders themselves are renamed. Nothing inside them is touched,
    so media files never move.
"""

import argparse
import os
import re
import shutil
import sys
import time

# This script lives in scripts/, so the app modules are one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import driveid     # noqa: E402
import scanner     # noqa: E402

# Matches a leading number and separator: "00_Anime" -> "Anime".
NUMERIC_PREFIX = re.compile(r"^\d+[_\-.\s]+")

# A library root with a drive name stuck on the end: "Videos_SSD3" -> "Videos".
LIBRARY_ROOT_NAME = "Videos"


def normalize_drive_arg(arg):
    """
    Turn what the user typed into a drive root path.

    Accepts a bare letter ("d"), a letter with a colon ("d:"), a root path
    ("D:\\", "D:/"), or any other path, which is passed through unchanged so
    a folder can still be given directly.
    """
    text = arg.strip().strip('"')
    if len(text) == 1 and text.isalpha():
        return f"{text.upper()}:\\"
    if len(text) == 2 and text[1] == ":" and text[0].isalpha():
        return f"{text[0].upper()}:\\"
    return text


def strip_prefix(name):
    return NUMERIC_PREFIX.sub("", name).strip()


def target_category_name(name):
    """New name for a category folder, or None if it does not need renaming."""
    new_name = strip_prefix(name)
    if not new_name or new_name == name:
        return None
    return new_name


def target_root_name(name):
    """
    New name for a library root, or None if it does not need renaming.

    Strips any numeric prefix, then collapses "Videos" plus a separator and a
    drive nickname ("Videos_SSD3", "Videos-SSD1", "00_Videos") down to plain
    "Videos". The drive letter already tells the drives apart, so the suffix
    adds nothing.

    The separator matters. "VideosArchive" is a different folder name, not a
    decorated "Videos", so it is left alone.
    """
    stripped = strip_prefix(name)
    if not stripped:
        return None

    prefix_len = len(LIBRARY_ROOT_NAME)
    if stripped.lower()[:prefix_len] != LIBRARY_ROOT_NAME.lower():
        return None

    remainder = stripped[prefix_len:]
    if remainder and remainder[0] not in "_-. ":
        return None
    if name == LIBRARY_ROOT_NAME:
        return None
    return LIBRARY_ROOT_NAME


def find_roots(drive_root, root_prefix, redundancy_prefix):
    """Every library and redundancy root on the drive, as (path, kind)."""
    root_prefixes = [p.strip().lower() for p in root_prefix.split(",") if p.strip()]
    roots = [
        (p, "library")
        for p in scanner.find_top_level_matches(
            drive_root, lambda n: any(n.lower().startswith(pre) for pre in root_prefixes)
        )
    ]
    if redundancy_prefix:
        roots += [
            (p, "redundancy")
            for p in scanner.find_top_level_matches(
                drive_root, lambda n: redundancy_prefix.lower() in n.lower()
            )
        ]
    return roots


def plan_renames(drive_root, root_prefix, redundancy_prefix, rename_roots=True):
    """
    Work out every rename on this drive without doing any of them.

    Returns (renames, conflicts, skipped). Each rename is a dict with keys
    root_path, kind, src, dst, what. Category renames for a root always come
    before that root's own rename, so applying them in order keeps the
    category paths valid.
    """
    renames, conflicts, skipped = [], [], []

    for root_path, kind in find_roots(drive_root, root_prefix, redundancy_prefix):
        try:
            with os.scandir(root_path) as entries:
                children = [e for e in entries if e.is_dir(follow_symlinks=False)]
        except OSError as e:
            skipped.append((root_path, f"could not list it: {e}"))
            continue

        for entry in sorted(children, key=lambda e: e.name):
            new_name = target_category_name(entry.name)
            if new_name is None:
                continue
            dst = os.path.join(root_path, new_name)
            if os.path.exists(dst):
                conflicts.append((entry.path, dst))
                continue
            renames.append({"root_path": root_path, "kind": kind, "src": entry.path,
                            "dst": dst, "what": "category"})

        # The root itself goes last, so the category renames above still point
        # at paths that exist when they run.
        if rename_roots and kind == "library":
            new_root = target_root_name(os.path.basename(root_path))
            if new_root is not None:
                dst = os.path.join(drive_root, new_root)
                if os.path.exists(dst):
                    conflicts.append((root_path, dst))
                else:
                    renames.append({"root_path": root_path, "kind": kind, "src": root_path,
                                    "dst": dst, "what": "root"})

    return renames, conflicts, skipped


def confirm(prompt):
    """
    Ask about one rename. Returns 'yes', 'no', 'all', or 'quit'.

    y and n decide just this one. 'a' accepts everything left on this drive
    without asking again, and 'q' stops the drive where it is. Anything else
    asks again. If there is no input available the answer is 'quit', so a
    piped or unattended run never renames anything by accident.
    """
    while True:
        try:
            answer = input(f"{prompt} [y/n/a=all/q=quit] ").strip().lower()
        except EOFError:
            print("\n  No input available, stopping. Use --yes to run unattended.")
            return "quit"
        if answer in ("y", "yes"):
            return "yes"
        if answer in ("n", "no"):
            return "no"
        if answer in ("a", "all"):
            return "all"
        if answer in ("q", "quit"):
            return "quit"


def rename_with_retry(src, dst, attempts=3, wait=0.5):
    """
    Rename a folder, retrying briefly if Windows says it is in use.

    On Windows a folder cannot be renamed while something holds a handle to
    it. A file browser, the search indexer, antivirus, or a media player with
    the library open will all cause this, usually only for a moment. Returns
    None on success or the last error message on failure.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return None
        except OSError as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(wait)
    return str(last_error)


def describe(rename, drive_root):
    label = "library root" if rename["what"] == "root" else "category"
    src_rel = os.path.relpath(rename["src"], drive_root)
    return f"    [{label}] {src_rel}  ->  {os.path.basename(rename['dst'])}"


def migrate_drive(drive_path, apply, keep_tags, root_prefix, redundancy_prefix,
                  assume_yes=False, rename_roots=True):
    drive_root = os.path.abspath(normalize_drive_arg(drive_path))
    if not os.path.isdir(drive_root):
        print(f"\n=== {drive_root} ===")
        print("  Not connected, or not a folder. Skipping.")
        return False

    renames, conflicts, skipped = plan_renames(
        drive_root, root_prefix, redundancy_prefix, rename_roots
    )

    print(f"\n=== {drive_root} ===")
    if not renames and not conflicts and not skipped:
        print("  Nothing to do. No numbered folder names found.")
        return True

    for src, dst in conflicts:
        print(f"  CONFLICT  {os.path.basename(src)} -> {os.path.basename(dst)} "
              f"already exists, skipping")
    for path, why in skipped:
        print(f"  SKIP  {path}: {why}")

    if not apply:
        for rename in renames:
            print(describe(rename, drive_root))
        print(f"  Dry run. Re-run with --apply to perform {len(renames)} rename(s).")
        return True

    done, declined, failed, current_root = 0, 0, 0, None
    accept_all = assume_yes

    for rename in renames:
        if rename["root_path"] != current_root:
            current_root = rename["root_path"]
            print(f"\n  --- {os.path.basename(current_root)} ({rename['kind']}) ---")

        line = describe(rename, drive_root)
        if not accept_all:
            choice = confirm(line + "\n    rename?")
            if choice == "quit":
                print("  Stopped.")
                break
            if choice == "no":
                declined += 1
                print("    skipped.")
                continue
            if choice == "all":
                accept_all = True
        else:
            print(line)

        error = rename_with_retry(rename["src"], rename["dst"])
        if error is None:
            done += 1
        else:
            failed += 1
            print(f"    FAILED: {error}")

    print(f"\n  Renamed {done} folder(s)" + (f", skipped {declined}." if declined else "."))
    if failed:
        print(f"  {failed} rename(s) failed. This usually means something has the "
              f"folder open.\n  Close anything using the drive, then re-run.\n"
              f"  Renames that already succeeded are kept, so it is safe to run again.")
    if done == 0:
        return True

    # Paths changed, so the indexed tree and the tag keys are both out of date.
    total = shutil.disk_usage(drive_root)[0]
    drive_id = driveid.get_drive_id(drive_root, total)

    if not keep_tags:
        removed = db.clear_tags_for_drive(drive_id)
        print(f"  Cleared {removed} stale tag row(s). The rescan puts the "
              f"category tags back.")

    print("  Rescanning...")
    scanner.scan_and_store(drive_root, log=lambda _m: None)
    print("  Done.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="One-off tool to remove numeric prefixes from folder names."
    )
    parser.add_argument("drives", nargs="+",
                        help="Drives to migrate. A bare letter works: c d e")
    parser.add_argument("--apply", action="store_true",
                        help="Actually rename. Without this it is a dry run.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the y/n prompts and accept every rename.")
    parser.add_argument("--keep-root-names", action="store_true",
                        help="Leave library roots alone, only rename category folders.")
    parser.add_argument("--keep-tags", action="store_true",
                        help="Keep existing tag rows instead of clearing and re-seeding them.")
    parser.add_argument("--root-prefix", default=scanner.DEFAULT_ROOT_PREFIX)
    parser.add_argument("--redundancy-prefix", default=scanner.DEFAULT_REDUNDANCY_PREFIX)
    args = parser.parse_args()

    if not args.apply:
        print("DRY RUN. Nothing will be modified. Add --apply to commit.")
    else:
        print("Close anything that has these drives open first.\n"
              "Windows cannot rename a folder that another program is using.\n")

    db.init_db()
    ok = True
    for drive in args.drives:
        ok = migrate_drive(drive, args.apply, args.keep_tags, args.root_prefix,
                           args.redundancy_prefix, args.yes,
                           rename_roots=not args.keep_root_names) and ok

    if args.apply:
        print("\nMigration finished. Point any media apps at the renamed folders.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
