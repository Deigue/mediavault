"""
moveops.py - moving a title from one drive to another.

A move between drives is never a rename. It is a copy, then a check that the
copy really arrived, and only then a delete of the original. If anything goes
wrong the source is left exactly where it was, so the worst outcome is a
half-written folder on the target that can be deleted and retried.

The copy itself is done by whichever tool is configured (robocopy, TeraCopy,
or a plain Python copy).
"""

import os
import shutil
import subprocess
import threading
import time

import config
import db
import scanner

# robocopy uses exit codes 0-7 for success (files copied, extra files found,
# and so on). 8 and above mean at least one file failed to copy.
ROBOCOPY_OK_MAX = 7


class MoveError(Exception):
    """Something went wrong that the user should be told about."""


def category_for(node, conn_nodes=None):
    """
    The category folder a title sits in, e.g. "Anime". Titles are at depth 2,
    so this is the name of their parent.
    """
    if node["parent_id"] is None:
        return None
    parent = db.get_node(node["parent_id"])
    return parent["name"] if parent else None


def plan_move(node_id, target_drive_id):
    """
    Work out where a title would go, without moving anything.

    Returns a dict describing the move, or raises MoveError explaining why it
    cannot be done. Both drives have to be connected, and the target must not
    already have something with that name in that category.
    """
    node = db.get_node(node_id)
    if node is None:
        raise MoveError("That title is no longer in the database. Rescan and try again.")
    if node["depth"] != 2:
        raise MoveError("Only titles can be moved, not whole categories or single "
                        "files inside a title.")

    source_drive = db.get_drive(node["drive_id"])
    target_drive = db.get_drive(target_drive_id)
    if target_drive is None:
        raise MoveError("No such target drive.")
    if node["drive_id"] == target_drive_id:
        raise MoveError(f"'{node['name']}' is already on {target_drive['label']}.")

    source_mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if source_mount is None:
        raise MoveError(f"'{source_drive['label']}' is not connected.")
    target_mount = scanner.find_mounted_drive(target_drive_id, max_age=0)
    if target_mount is None:
        raise MoveError(f"'{target_drive['label']}' is not connected.")

    source_path = os.path.abspath(os.path.join(source_mount, node["rel_path"]))
    if not os.path.exists(source_path):
        raise MoveError(f"Not on disk any more:\n{source_path}")

    category = category_for(node)
    if not category:
        raise MoveError(f"Could not work out which category '{node['name']}' is in.")

    library_root = find_library_root(target_mount)
    if library_root is None:
        raise MoveError(f"'{target_drive['label']}' has no library folder. "
                        f"Set it up first.")

    target_dir = os.path.join(library_root, category)
    target_path = os.path.join(target_dir, node["name"])
    if os.path.exists(target_path):
        raise MoveError(f"'{node['name']}' already exists in {category} on "
                        f"{target_drive['label']}. Nothing was moved.")

    size = node["size_bytes"] or 0
    free = shutil.disk_usage(target_mount).free
    if size > free:
        raise MoveError(
            f"Not enough room on {target_drive['label']}: needs "
            f"{size / 2**30:.1f} GB, has {free / 2**30:.1f} GB free."
        )

    return {
        "node_id": node_id,
        "name": node["name"],
        "category": category,
        "size_bytes": size,
        "is_dir": bool(node["is_dir"]),
        "source_drive_id": node["drive_id"],
        "source_label": source_drive["label"],
        "source_path": source_path,
        "target_drive_id": target_drive_id,
        "target_label": target_drive["label"],
        "target_dir": target_dir,
        "target_path": target_path,
        "rel_path_after": os.path.relpath(target_path, target_mount),
        "target_mount": target_mount,
        "tags": db.get_tags_for_drive(node["drive_id"]).get(node["rel_path"], []),
    }


DEFAULT_REDUNDANCY_ROOT = "99_Redundancy"


def find_or_make_redundancy_root(mount, redundancy_prefix=None):
    """
    The drive's backup folder, created if it has none.

    An existing folder wins whatever it is called, so a drive already using
    "Redundancy_Backup" keeps it rather than gaining a second one.
    """
    prefix = (redundancy_prefix or scanner.DEFAULT_REDUNDANCY_PREFIX).lower()
    existing = scanner.find_top_level_matches(mount, lambda n: prefix in n.lower())
    if existing:
        return existing[0], False

    path = os.path.join(mount, DEFAULT_REDUNDANCY_ROOT)
    os.makedirs(path, exist_ok=True)
    return path, True


def plan_redundancy_copy(node_id, target_drive_id):
    """
    Work out where a backup copy of a title would go, without copying.

    The copy lands in the target's redundancy folder, under the same category
    the title is in now. Both the redundancy folder and the category are
    created if they are missing.
    """
    node = db.get_node(node_id)
    if node is None:
        raise MoveError("That title is no longer in the database. Rescan and try again.")
    if node["depth"] != 2:
        raise MoveError("Only whole titles can be backed up.")

    source_drive = db.get_drive(node["drive_id"])
    target_drive = db.get_drive(target_drive_id)
    if target_drive is None:
        raise MoveError("No such target drive.")

    source_mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if source_mount is None:
        raise MoveError(f"'{source_drive['label']}' is not connected.")
    target_mount = scanner.find_mounted_drive(target_drive_id, max_age=0)
    if target_mount is None:
        raise MoveError(f"'{target_drive['label']}' is not connected.")

    if node["drive_id"] == target_drive_id:
        # Allowed, but say what it does and does not protect against.
        pass

    source_path = os.path.abspath(os.path.join(source_mount, node["rel_path"]))
    if not os.path.exists(source_path):
        raise MoveError(f"Not on disk any more:\n{source_path}")

    category = category_for(node)
    if not category:
        raise MoveError(f"Could not work out which category '{node['name']}' is in.")

    redundancy_root, created_root = find_or_make_redundancy_root(target_mount)
    target_dir = os.path.join(redundancy_root, category)
    target_path = os.path.join(target_dir, node["name"])

    if os.path.exists(target_path):
        raise MoveError(f"A backup of '{node['name']}' already exists in "
                        f"{os.path.basename(redundancy_root)}/{category} on "
                        f"{target_drive['label']}.")

    size = node["size_bytes"] or 0
    free = shutil.disk_usage(target_mount).free
    if size > free:
        raise MoveError(
            f"Not enough room on {target_drive['label']}: needs "
            f"{size / 2**30:.1f} GB, has {free / 2**30:.1f} GB free."
        )

    return {
        "operation": "copy",
        "node_id": node_id,
        "name": node["name"],
        "category": category,
        "size_bytes": size,
        "is_dir": bool(node["is_dir"]),
        "source_drive_id": node["drive_id"],
        "source_label": source_drive["label"],
        "source_path": source_path,
        "target_drive_id": target_drive_id,
        "target_label": target_drive["label"],
        "target_dir": target_dir,
        "target_path": target_path,
        "target_mount": target_mount,
        "redundancy_root": os.path.basename(redundancy_root),
        "created_redundancy_root": created_root,
        "same_drive": node["drive_id"] == target_drive_id,
    }


def copy_title(plan, log=None, progress=None):
    """
    Make a backup copy of a title. The source is never touched.

    A copy that fails verification is left in place rather than deleted, so
    it can be inspected, and the next attempt will refuse to overwrite it.
    """
    log = log or (lambda _m: None)
    source, target = plan["source_path"], plan["target_path"]

    log(f"  {plan['name']}")
    log(f"    {plan['source_label']} -> {plan['target_label']} / "
        f"{plan['redundancy_root']} / {plan['category']}")
    if plan.get("created_redundancy_root"):
        log(f"    created {plan['redundancy_root']} on {plan['target_label']}")
    if plan.get("same_drive"):
        log("    note: same drive, so this protects against deleting it by "
            "accident but not against the drive failing")

    os.makedirs(plan["target_dir"], exist_ok=True)
    copy_tree(source, target, plan["is_dir"], log, progress)

    if config.load().get("verify_after_copy", True):
        verify_copy(source, target, log)
    else:
        log("    verification is turned off in config.json")

    return {
        "name": plan["name"],
        "category": plan["category"],
        "size_bytes": plan["size_bytes"],
        "target_label": plan["target_label"],
    }


def find_library_root(mount, root_prefix=None):
    """The drive's library folder, e.g. D:\\Videos, or None if it has none."""
    prefixes = [p.strip().lower()
                for p in (root_prefix or scanner.DEFAULT_ROOT_PREFIX).split(",")
                if p.strip()]
    roots = scanner.find_top_level_matches(
        mount, lambda n: any(n.lower().startswith(p) for p in prefixes)
    )
    return roots[0] if roots else None


def _copy_with_robocopy(source, target, is_dir, log):
    """robocopy only copies directories, so a single file is handled as
    'this one file out of its parent folder'."""
    if is_dir:
        args = ["robocopy", source, target, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]
    else:
        args = ["robocopy", os.path.dirname(source), os.path.dirname(target),
                os.path.basename(source), "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode > ROBOCOPY_OK_MAX:
        raise MoveError(f"robocopy failed (exit {result.returncode}): "
                        f"{(result.stdout or result.stderr or '').strip()[:400]}")
    log(f"    robocopy finished (exit {result.returncode})")


# Command lines for the external copiers, as (args builder, friendly name).
def _external_args(tool, tool_path, source, target_parent):
    if tool == "fastcopy":
        return [tool_path, "/cmd=diff", "/auto_close", f"/to={target_parent}", source]
    # TeraCopy, and anything custom, take: <exe> Copy <source> <target dir>
    return [tool_path, "Copy", source, target_parent, "/Close"]


def _copy_with_external(tool, source, target, is_dir, tool_path, log, expected_bytes):
    """
    Hand the copy to an external program.

    These show their own progress window, which is usually the reason for
    choosing one. They also tend to pass the job to an instance that is
    already running and exit immediately, so the process finishing is not a
    signal that the copy is done. The target is watched instead.
    """
    args = _external_args(tool, tool_path, source, os.path.dirname(target))
    log(f"    handing off to {os.path.basename(tool_path)}, its window shows the detail")
    try:
        subprocess.Popen(args).wait()
    except OSError as e:
        raise MoveError(f"Could not start {tool_path}: {e}")

    wait_until_settled(target, expected_bytes, log)
    log(f"    {os.path.basename(tool_path)} finished")


def wait_until_settled(target, expected_bytes, log, idle_timeout=120, poll=1.0):
    """
    Wait for a copy to stop changing.

    Returns once the target reaches the expected size, or once it has not
    grown for idle_timeout seconds. The idle case is not treated as an error
    here: verify_copy() is what decides whether the result is acceptable.
    """
    last_size, last_change = -1, time.time()
    while True:
        size = measure(target)[1] if os.path.exists(target) else 0
        if expected_bytes and size >= expected_bytes:
            return
        if size != last_size:
            last_size, last_change = size, time.time()
        elif time.time() - last_change > idle_timeout:
            log(f"    target stopped growing at {size:,} bytes")
            return
        time.sleep(poll)


def _copy_with_python(source, target, is_dir, log):
    if is_dir:
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    log("    copied")


def copy_tree(source, target, is_dir, log, progress=None):
    """
    Copy source to target using the configured tool.

    progress, if given, is called with (files_done, total_files, bytes_done,
    total_bytes) roughly once a second. It works by measuring the target as
    it fills rather than by parsing tool output, so the same progress is
    reported whichever copier is in use. Without this a large title looks
    frozen for the whole copy, since one title can be hundreds of gigabytes.
    """
    settings = config.load()
    tool, path, note = config.resolve_copy_tool(settings)
    if note:
        log("    " + note)

    total_files, total_bytes = measure(source)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    log(f"    copying {total_files} file(s), {total_bytes:,} bytes, with {tool}")

    stop = threading.Event()
    watcher = None
    if progress:
        progress(0, total_files, 0, total_bytes)

        def watch():
            while not stop.wait(1.0):
                try:
                    if os.path.exists(target):
                        files, done = measure(target)
                    else:
                        files, done = 0, 0
                    progress(files, total_files, done, total_bytes)
                except OSError:
                    continue

        watcher = threading.Thread(target=watch, name="mediavault-copy-progress", daemon=True)
        watcher.start()

    try:
        if tool == "robocopy":
            _copy_with_robocopy(source, target, is_dir, log)
        elif tool in ("teracopy", "fastcopy", "custom"):
            # All driven the same way: hand the job over, then wait for the
            # target to stop changing, since these tools tend to return
            # before the copy is actually finished.
            _copy_with_external(tool, source, target, is_dir, path, log, total_bytes)
        else:
            _copy_with_python(source, target, is_dir, log)
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=3)

    if progress:
        files, done = measure(target) if os.path.exists(target) else (0, 0)
        progress(files, total_files, done, total_bytes)


def measure(path):
    """(file count, total bytes) for a file or a whole folder."""
    if os.path.isfile(path):
        return 1, os.path.getsize(path)
    files, total = 0, 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return files, total


def verify_copy(source, target, log):
    """
    Confirm the copy arrived before anything is deleted.

    Compares file count and total size. That catches the failures that
    actually happen here, a copy that stopped partway or ran out of room,
    without the cost of hashing hundreds of gigabytes.
    """
    if not os.path.exists(target):
        raise MoveError("The copy did not arrive: nothing at the target path.")

    source_files, source_bytes = measure(source)
    target_files, target_bytes = measure(target)
    log(f"    source {source_files} files / {source_bytes:,} bytes")
    log(f"    target {target_files} files / {target_bytes:,} bytes")

    if target_files != source_files or target_bytes != source_bytes:
        raise MoveError(
            f"The copy does not match the source, so nothing was deleted.\n"
            f"Source: {source_files} files, {source_bytes:,} bytes.\n"
            f"Target: {target_files} files, {target_bytes:,} bytes.\n"
            f"The partial copy is still at {target}."
        )
    log("    verified")


def move_title(plan, log=None, progress=None):
    """
    Carry out a planned move: copy, verify, then delete the source.

    The source is only removed once the copy has been checked. Tags follow
    the title to its new path.
    """
    log = log or (lambda _m: None)
    source, target = plan["source_path"], plan["target_path"]

    log(f"  {plan['name']}")
    log(f"    {plan['source_label']} -> {plan['target_label']} / {plan['category']}")

    copy_tree(source, target, plan["is_dir"], log, progress)

    settings = config.load()
    if settings.get("verify_after_copy", True):
        verify_copy(source, target, log)
    else:
        log("    verification is turned off in config.json")

    # Only now is it safe to remove the original.
    try:
        if plan["is_dir"]:
            shutil.rmtree(source)
        else:
            os.remove(source)
    except OSError as e:
        raise MoveError(
            f"Copied successfully, but the original could not be deleted: {e}\n"
            f"The title now exists in both places. Remove the source by hand:\n"
            f"{source}"
        )
    log("    source removed")

    # Carry the tags across, then drop the old rows.
    if plan["tags"]:
        db.set_tag_bulk([(plan["target_drive_id"], plan["rel_path_after"])],
                        plan["tags"][0], "add")
        for tag in plan["tags"][1:]:
            db.set_tag_bulk([(plan["target_drive_id"], plan["rel_path_after"])], tag, "add")
    db.delete_node_subtree(plan["node_id"])

    return {
        "name": plan["name"],
        "category": plan["category"],
        "size_bytes": plan["size_bytes"],
        "target_label": plan["target_label"],
    }
